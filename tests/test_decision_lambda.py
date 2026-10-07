"""Handler tests for backend/lambdas/decisions/app.py.

The decisions Lambda is the write half of the loop: it recomputes a
recommendation with the REAL engine (bundled byte-identical from
optimization/, see tests/test_engine_parity.py), then guards every
lifecycle transition proposed -> approved -> verified/failed.

The expected recommendation in these tests is derived via the repo's
own optimization package directly - the wire/DB logic is what is
under test here, not the cost model (that has its own pinned tests).
"""

import importlib.util
import json
import sys
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

REPO_ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(REPO_ROOT))

from optimization.adapters import from_aggregate_item
from optimization.optimizer import optimize_document

DECISIONS_LAMBDA_DIR = (
    REPO_ROOT / "backend" / "lambdas" / "decisions"
)
LAMBDA_PATH = DECISIONS_LAMBDA_DIR / "app.py"


@pytest.fixture()
def app(monkeypatch):
    """Load the decisions Lambda with stubbed AWS clients."""

    monkeypatch.setenv("DOCUMENTS_BUCKET", "test-bucket")
    monkeypatch.setenv("DOCUMENTS_TABLE", "DocumentsTable")
    monkeypatch.setenv("AGGREGATES_TABLE", "AggregatesTable")
    monkeypatch.setenv("DECISIONS_TABLE", "DecisionsTable")

    s3 = MagicMock()
    dynamodb = MagicMock()
    documents = MagicMock()
    aggregates = MagicMock()
    decisions = MagicMock()

    dynamodb.Table.side_effect = lambda name: {
        "DocumentsTable": documents,
        "AggregatesTable": aggregates,
        "DecisionsTable": decisions,
    }[name]

    with patch(
        "boto3.client", return_value=s3
    ), patch("boto3.resource", return_value=dynamodb):

        spec = importlib.util.spec_from_file_location(
            "decisions",
            LAMBDA_PATH,
            submodule_search_locations=[str(DECISIONS_LAMBDA_DIR)],
        )

        module = importlib.util.module_from_spec(spec)

        sys.modules["decisions"] = module

        # app.py imports the vendored engine by absolute name (the
        # Lambda runtime imports app.py as a bare module, where a
        # relative `from .engine` is illegal). Mirror the runtime's
        # sys.path: the CodeUri dir root is the task root.
        sys.path.insert(0, str(DECISIONS_LAMBDA_DIR))
        spec.loader.exec_module(module)

    module._test = MagicMock()
    module._test.s3 = s3
    module._test.documents = documents
    module._test.aggregates = aggregates
    module._test.decisions = decisions
    # The transactional proposal path rides the resource's low-level
    # client; expose the mock so tests can assert the transaction.
    module._test.transact = dynamodb.meta.client

    return module


# A cold 10 GB ARCHIVED document: the engine's answer for this
# fixture is computed through the repo package, not hardcoded.
COLD_AGGREGATE = {
    "document_id": "doc-9",
    "file_size_bytes": 10_000_000_000,
    "current_storage_class": "STANDARD",
    "upload_timestamp": "2026-01-01T00:00:00Z",
    "last_accessed_timestamp": "2026-02-01T00:00:00Z",
    "access_count": 0,
    "days_since_last_access": 240.0,
    "access_frequency": 0.0,
    "aggregation_timestamp": "2026-04-01T00:00:00Z",
    "document_state": "ARCHIVED",
}

COLD_DOCUMENT = {
    "document_id": "doc-9",
    "object_key": "documents/doc-9/record.pdf",
    "file_name": "record.pdf",
    "file_size": 10_000_000_000,
    "current_storage_class": "STANDARD",
}

# The engine's expected answer for the cold fixture, computed the
# same way the lambda computes it (same adapter, same optimizer).
COLD_EXPECTED = optimize_document(
    from_aggregate_item(COLD_AGGREGATE), "B"
)


def api_event(method, path, body=None, query=None, claims=None):
    event = {
        "rawPath": path,
        "requestContext": {"http": {"method": method}},
    }

    if body is not None:
        event["body"] = json.dumps(body)

    if query is not None:
        event["queryStringParameters"] = query

    if claims is not None:
        event["requestContext"]["authorizer"] = {
            "jwt": {"claims": claims}
        }

    return event


def body(response):
    return json.loads(response["body"])


def wire_cold_fixtures(app):
    app._test.aggregates.get_item.return_value = {"Item": dict(COLD_AGGREGATE)}
    app._test.documents.get_item.return_value = {"Item": dict(COLD_DOCUMENT)}
    app._test.decisions.query.return_value = {"Items": []}


# ---------------------------------------------------------------
# Routing
# ---------------------------------------------------------------

def test_unknown_route_404(app):
    assert (
        app.lambda_handler(api_event("GET", "/nope"), None)["statusCode"]
        == 404
    )
    # POST /ledger is not a route.
    assert (
        app.lambda_handler(api_event("POST", "/ledger"), None)["statusCode"]
        == 404
    )


def test_execute_never_answers_get(app):
    """A GET on the execute route must 405 before any S3 copy - a
    browser prefetch must never fire a storage transition."""

    app._test.s3.copy_object.assert_not_called()

    result = app.lambda_handler(
        api_event("GET", "/decisions/d-1/execute"), None
    )

    assert result["statusCode"] == 405
    app._test.s3.copy_object.assert_not_called()


# ---------------------------------------------------------------
# create_decision
# ---------------------------------------------------------------

def test_create_happy_path_proposes_a_pending_decision(app):
    wire_cold_fixtures(app)

    result = app.lambda_handler(
        api_event("POST", "/decisions", {"document_id": "doc-9"}), None
    )

    assert result["statusCode"] == 201

    payload = body(result)

    # The engine's recommendation drives the record - not a
    # hand-pinned guess.
    assert payload["to_class"] == COLD_EXPECTED.recommended_storage_class
    assert payload["from_class"] == "STANDARD"
    assert payload["predicted_savings"] == pytest.approx(
        COLD_EXPECTED.savings, abs=1e-9
    )
    assert payload["decision_state"] == "proposed"
    assert payload["features_as_of"] == COLD_AGGREGATE["aggregation_timestamp"]

    # Sampling provenance: the record carries the Poisson band of
    # the forecast and the count band that produced it.
    assert payload["predicted_savings_low"] >= 0
    assert payload["predicted_savings_high"] >= payload["predicted_savings"]
    assert payload["forecast_access_band"] == [0, 4]  # k=0: (0, -ln 0.025)=3.6889 -> ceil 4

    transact = app._test.transact.transact_write_items.call_args.kwargs
    items = transact["TransactItems"]

    # The write is transactional: open-decision marker (guarded) +
    # the decision itself, so two concurrent proposals cannot both
    # pass.
    assert len(items) == 2

    marker = items[0]["Put"]
    decision = items[1]["Put"]

    assert marker["Item"]["decision_id"] == "OPENDEC#doc-9"
    assert marker["ConditionExpression"] == "attribute_not_exists(decision_id)"

    written = decision["Item"]

    # DynamoDB writes: floats must be Decimal so the write would
    # succeed against the real table.
    assert isinstance(written["predicted_savings"], Decimal)
    assert isinstance(written["predicted_savings_low"], Decimal)
    assert isinstance(written["engine_input"]["access_frequency"], Decimal)
    assert written["decision_state"] == "proposed"


def test_create_rejects_empty_or_bad_body(app):
    for event in (
        api_event("POST", "/decisions", None),
        api_event("POST", "/decisions", {"document_id": "  "}),
        api_event("POST", "/decisions", {"document_id": 7}),
    ):
        assert app.lambda_handler(event, None)["statusCode"] == 400


def test_create_without_aggregate_is_409(app):
    wire_cold_fixtures(app)
    app._test.aggregates.get_item.return_value = {"Item": None}

    result = app.lambda_handler(
        api_event("POST", "/decisions", {"document_id": "doc-9"}), None
    )

    assert result["statusCode"] == 409
    assert "aggregate" in body(result)["error"]
    app._test.transact.transact_write_items.assert_not_called()


def test_create_when_engine_says_keep_in_place_is_409(app):
    """A hot Standard document gets no migration - the queue must
    only ever hold actionable decisions."""

    wire_cold_fixtures(app)

    hot_aggregate = dict(COLD_AGGREGATE)
    hot_aggregate.update(
        {
            "access_count": 90,
            "access_frequency": 3.0,
            "days_since_last_access": 1.0,
            "last_accessed_timestamp": "2026-04-01T00:00:00Z",
        }
    )

    hot_expected = optimize_document(
        from_aggregate_item(hot_aggregate), "B"
    )

    assert hot_expected.recommended_storage_class == "STANDARD"

    app._test.aggregates.get_item.return_value = {"Item": hot_aggregate}

    result = app.lambda_handler(
        api_event("POST", "/decisions", {"document_id": "doc-9"}), None
    )

    assert result["statusCode"] == 409
    assert "no migration" in body(result)["error"]
    app._test.transact.transact_write_items.assert_not_called()


def test_create_refuses_a_duplicate_pending_decision(app):
    """One open decision per document: approving twice must never
    mean one document migrates twice."""

    wire_cold_fixtures(app)

    for state in ("proposed", "approved"):
        app._test.decisions.put_item.reset_mock()
        app._test.decisions.query.return_value = {
            "Items": [
                {
                    "decision_id": "d-existing",
                    "decision_state": state,
                }
            ]
        }

        result = app.lambda_handler(
            api_event("POST", "/decisions", {"document_id": "doc-9"}), None
        )

        assert result["statusCode"] == 409
        assert body(result)["decision_id"] == "d-existing"
        app._test.transact.transact_write_items.assert_not_called()


# ---------------------------------------------------------------
# approve / reject transitions
# ---------------------------------------------------------------

def proposed_decision(app):
    return {
        "decision_id": "d-1",
        "document_id": "doc-9",
        "decision_state": "proposed",
    }


def test_approve_and_reject_move_from_proposed(app):
    app._test.decisions.get_item.return_value = {
        "Item": proposed_decision(app)
    }

    result = app.lambda_handler(
        api_event("POST", "/decisions/d-1/approve"), None
    )

    assert result["statusCode"] == 200

    kwargs = app._test.decisions.update_item.call_args.kwargs

    assert kwargs["ExpressionAttributeValues"][":state"] == "approved"
    # approved_at is a reserved-word-adjacent attribute name; it
    # must ride through the name map.
    assert "#stamp_attr" in kwargs["UpdateExpression"]
    assert (
        kwargs["ExpressionAttributeNames"]["#stamp_attr"]
        == "approved_at"
    )


def test_reject_writes_the_rejected_stamp(app):
    app._test.decisions.get_item.return_value = {
        "Item": proposed_decision(app)
    }

    result = app.lambda_handler(
        api_event("POST", "/decisions/d-1/reject"), None
    )

    assert result["statusCode"] == 200

    kwargs = app._test.decisions.update_item.call_args.kwargs

    assert kwargs["ExpressionAttributeValues"][":state"] == "rejected"
    assert (
        kwargs["ExpressionAttributeNames"]["#stamp_attr"]
        == "rejected_at"
    )


def test_approve_refuses_a_non_proposed_decision(app):
    app._test.decisions.get_item.return_value = {
        "Item": {**proposed_decision(app), "decision_state": "verified"}
    }

    result = app.lambda_handler(
        api_event("POST", "/decisions/d-1/approve"), None
    )

    assert result["statusCode"] == 409
    app._test.decisions.update_item.assert_not_called()


def test_approve_unknown_decision_404(app):
    app._test.decisions.get_item.return_value = {"Item": None}

    assert (
        app.lambda_handler(
            api_event("POST", "/decisions/ghost/approve"), None
        )["statusCode"]
        == 404
    )


# ---------------------------------------------------------------
# execute -> verify
# ---------------------------------------------------------------

def approved_decision(app):
    decision = proposed_decision(app)
    decision.update(
        {
            "decision_state": "approved",
            "object_key": "documents/doc-9/record.pdf",
            "to_class": "GLACIER_DEEP_ARCHIVE",
            "predicted_savings": Decimal("12.5"),
        }
    )
    return decision


def test_execute_copies_verifies_and_realizes(app):
    app._test.decisions.get_item.return_value = {
        "Item": approved_decision(app)
    }
    # First call: the pre-transition HEAD (sizes the copy); second:
    # the verification HEAD.
    app._test.s3.head_object.side_effect = [
        {"StorageClass": "STANDARD", "ContentLength": 1_000_000_000},
        # S3 reports the API enum, not the engine billing name.
        {"StorageClass": "DEEP_ARCHIVE", "ContentLength": 1_000_000_000},
    ]

    result = app.lambda_handler(
        api_event("POST", "/decisions/d-1/execute"), None
    )

    assert result["statusCode"] == 200

    payload = body(result)

    assert payload["decision_state"] == "verified"
    # The decision record keeps the engine billing name the proposal
    # was priced under, not the API enum S3 reported back.
    assert payload["verified_storage_class"] == "GLACIER_DEEP_ARCHIVE"
    assert payload["realized_savings"] == pytest.approx(12.5)

    # The atomic claim rode in front of the copy.
    claim = app._test.decisions.update_item.call_args_list[0].kwargs
    assert claim["ConditionExpression"] == "decision_state = :expected"
    assert claim["ExpressionAttributeValues"][":expected"] == "approved"

    copy_kwargs = app._test.s3.copy_object.call_args.kwargs

    # The copy moves the object onto itself into the approved class,
    # translated to the S3 API enum the engine billing name maps to.
    assert copy_kwargs["StorageClass"] == "DEEP_ARCHIVE"
    assert copy_kwargs["Key"] == "documents/doc-9/record.pdf"
    assert copy_kwargs["CopySource"] == {
        "Bucket": "test-bucket",
        "Key": "documents/doc-9/record.pdf",
    }

    update = app._test.decisions.update_item.call_args_list[1].kwargs

    assert update["ExpressionAttributeValues"][":state"] == "verified"
    assert "REMOVE execute_claim" in update["UpdateExpression"]
    assert isinstance(
        update["ExpressionAttributeValues"][":savings"], Decimal
    )

    # The open-decision marker is released, so the document can be
    # proposed again as a NEW decision against post-move state.
    delete_key = app._test.decisions.delete_item.call_args.kwargs["Key"]
    assert delete_key == {"decision_id": "OPENDEC#doc-9"}


def test_execute_maps_archive_billing_names_to_s3_api_enums(app):
    """Regression for the live InvalidStorageClass failure: the
    engine prices plans under AWS *billing* names, but CopyObject
    will only accept the API enum — GLACIER_INSTANT_RETRIEVAL is
    GLACIER_IR to S3. The boundary must translate out and back, so
    S3 gets the enum and the record keeps the billing name."""

    decision = approved_decision(app)
    decision.update(
        {
            "to_class": "GLACIER_INSTANT_RETRIEVAL",
            "predicted_savings": Decimal("3.25"),
        }
    )
    app._test.decisions.get_item.return_value = {"Item": decision}

    app._test.s3.head_object.side_effect = [
        {"StorageClass": "STANDARD", "ContentLength": 1_000_000_000},
        {"StorageClass": "GLACIER_IR", "ContentLength": 1_000_000_000},
    ]

    result = app.lambda_handler(
        api_event("POST", "/decisions/d-1/execute"), None
    )

    assert result["statusCode"] == 200

    payload = body(result)

    assert payload["decision_state"] == "verified"
    assert payload["verified_storage_class"] == (
        "GLACIER_INSTANT_RETRIEVAL"
    )

    copy_kwargs = app._test.s3.copy_object.call_args.kwargs
    assert copy_kwargs["StorageClass"] == "GLACIER_IR"


def test_storage_class_boundary_mapping_round_trips(app):
    for engine_name, api_name in (
        app.S3_API_CLASS_BY_ENGINE_NAME.items()
    ):
        assert app.ENGINE_CLASS_BY_S3_API_NAME[api_name] == engine_name

    # Pin the exact pairs: flexible retrieval is GLACIER to S3 and
    # deep archive is DEEP_ARCHIVE — API spellings that no billing
    # name uses.
    assert app.S3_API_CLASS_BY_ENGINE_NAME == {
        "GLACIER_INSTANT_RETRIEVAL": "GLACIER_IR",
        "GLACIER_FLEXIBLE_RETRIEVAL": "GLACIER",
        "GLACIER_DEEP_ARCHIVE": "DEEP_ARCHIVE",
    }

    # The standard tiers share one vocabulary; nothing may pretend
    # they are distinct names that need translating.
    for tier in (
        "STANDARD", "STANDARD_IA", "ONEZONE_IA", "INTELLIGENT_TIERING",
    ):
        assert tier not in app.S3_API_CLASS_BY_ENGINE_NAME


def test_propose_batch_skips_archived_sources(app):
    """An object in flexible/deep archive cannot be re-copied until
    S3 restores it, so queueing such a proposal would only fail at
    execution - the proposer skips it and says why."""

    archived = {
        **COLD_AGGREGATE,
        "current_storage_class": "GLACIER_DEEP_ARCHIVE",
        # Regular access keeps the engine recommending a real
        # migration rather than staying put.
        "access_count": 3,
        "days_since_last_access": 8.0,
        "access_frequency": 0.1,
    }
    archived_doc = {
        **COLD_DOCUMENT,
        "current_storage_class": "GLACIER_DEEP_ARCHIVE",
    }
    app._test.aggregates.scan.return_value = {"Items": [archived]}
    app._test.documents.scan.return_value = {
        "Items": [archived_doc]
    }
    app._test.decisions.query.side_effect = [
        {"Items": []}, {"Items": []}, {"Items": []},
    ]

    result = app.lambda_handler(
        api_event("POST", "/decisions/propose-batch", {"max": 5}), None
    )

    payload = body(result)

    assert payload["proposed_count"] == 0
    assert "archived in GLACIER_DEEP_ARCHIVE" in (
        payload["skipped"][0]["reason"]
    )
    app._test.transact.transact_write_items.assert_not_called()


def test_execute_allows_recopying_out_of_glacier_instant(app):
    """GLACIER_IR keeps instant access, so an object sitting in it
    is NOT restore-gated: the executor verifies the re-tier instead
    of failing it."""

    decision = approved_decision(app)
    decision.update({"to_class": "GLACIER_INSTANT_RETRIEVAL"})
    app._test.decisions.get_item.return_value = {"Item": decision}

    # The pre-transition HEAD reveals an archived object.
    app._test.s3.head_object.side_effect = [
        {"StorageClass": "GLACIER_IR", "ContentLength": 1_000_000},
        {"StorageClass": "GLACIER_IR", "ContentLength": 1_000_000},
    ]

    result = app.lambda_handler(
        api_event("POST", "/decisions/d-1/execute"), None
    )

    payload = body(result)

    assert result["statusCode"] == 200
    assert payload["decision_state"] == "verified"
    assert payload["verified_storage_class"] == "GLACIER_INSTANT_RETRIEVAL"


def test_execute_fails_when_pre_head_finds_a_true_archive(app):
    """If the object really moved into flexible/deep archive (a
    restore-pending re-tier, say), the executor must NOT claim
    savings: transition returns a clear restore error and the
    decision ends as failed."""

    decision = approved_decision(app)
    decision.update({
        "to_class": "GLACIER_INSTANT_RETRIEVAL",
        "document_id": "synthetic-0419",
    })
    app._test.decisions.get_item.return_value = {"Item": decision}

    app._test.s3.head_object.side_effect = [
        # Pre-HEAD: the object is genuinely archived now.
        {"StorageClass": "GLACIER", "ContentLength": 256_000_000},
    ]

    result = app.lambda_handler(
        api_event("POST", "/decisions/d-1/execute"), None
    )

    payload = body(result)

    assert result["statusCode"] == 200
    assert payload["decision_state"] == "failed"
    assert "archived in GLACIER_FLEXIBLE_RETRIEVAL" in (
        payload["verification_error"]
    )
    assert "restore" in payload["verification_error"]

    # Nothing was written as realized savings.
    app._test.s3.copy_object.assert_not_called()
    app._test.documents.update_item.assert_not_called()
    # The open decision is released so the document can be
    # re-proposed once the restore completes.
    delete_key = app._test.decisions.delete_item.call_args.kwargs["Key"]
    assert delete_key == {"decision_id": "OPENDEC#synthetic-0419"}


def test_execute_over_five_gib_uses_multipart_copy(app):
    """copy_object tops out at 5 GiB; the executor must switch to a
    multipart transition so real archive-size objects still move."""

    app._test.decisions.get_item.return_value = {
        "Item": approved_decision(app)
    }

    six_gib = 6_442_450_944  # 6 GiB
    app._test.s3.head_object.side_effect = [
        {"StorageClass": "STANDARD", "ContentLength": six_gib},
        {"StorageClass": "DEEP_ARCHIVE", "ContentLength": six_gib},
    ]
    app._test.s3.create_multipart_upload.return_value = {
        "UploadId": "upload-1"
    }
    app._test.s3.upload_part_copy.return_value = {
        "CopyPartResult": {"ETag": '"etag"'}
    }
    app._test.s3.complete_multipart_upload.return_value = {}

    result = app.lambda_handler(
        api_event("POST", "/decisions/d-1/execute"), None
    )

    assert result["statusCode"] == 200
    assert body(result)["decision_state"] == "verified"

    # Plain copy_object never fires; parts stream under the upload.
    app._test.s3.copy_object.assert_not_called()

    create_kwargs = (
        app._test.s3.create_multipart_upload.call_args.kwargs
    )
    assert create_kwargs["StorageClass"] == "DEEP_ARCHIVE"

    part_args = [
        call.kwargs
        for call in app._test.s3.upload_part_copy.call_args_list
    ]
    # Workers record the mock in completion order, not part order.
    part_args.sort(key=lambda p: p["PartNumber"])

    # 6 GiB / 256 MiB = 24 contiguous inclusive ranges, 1-based
    # part numbers.
    assert len(part_args) == 24
    assert [p["PartNumber"] for p in part_args[0:2]] == [1, 2]
    assert part_args[0]["CopySourceRange"] == "bytes=0-268435455"
    assert part_args[-1]["CopySourceRange"] == (
        f"bytes={6_442_450_944 - 268_435_456}-{six_gib - 1}"
    )

    complete = app._test.s3.complete_multipart_upload.call_args.kwargs
    assert complete["UploadId"] == "upload-1"
    assert len(complete["MultipartUpload"]["Parts"]) == 24

    app._test.s3.abort_multipart_upload.assert_not_called()


def test_execute_multipart_failure_aborts_and_records_failed(app):
    app._test.decisions.get_item.return_value = {
        "Item": approved_decision(app)
    }

    app._test.s3.head_object.side_effect = [
        {"StorageClass": "STANDARD", "ContentLength": 6 * 1024**3},
    ]
    app._test.s3.create_multipart_upload.return_value = {
        "UploadId": "upload-1"
    }
    app._test.s3.upload_part_copy.side_effect = RuntimeError("part boom")

    result = app.lambda_handler(
        api_event("POST", "/decisions/d-1/execute"), None
    )

    assert result["statusCode"] == 200

    body = json.loads(result["body"])

    assert body["decision_state"] == "failed"
    assert "part boom" in body["verification_error"]

    # The aborted upload leaves no billed orphan parts.
    app._test.s3.abort_multipart_upload.assert_called_once()


def test_execute_lost_the_claim_race_returns_409(app):
    """Two concurrent executes of the same approved decision: the
    conditional claim admits exactly one; the loser 409s without
    ever touching S3."""

    app._test.decisions.get_item.return_value = {
        "Item": approved_decision(app)
    }
    app._test.decisions.update_item.side_effect = ClientError(
        {
            "Error": {
                "Code": "ConditionalCheckFailedException",
                "Message": "condition failed",
            }
        },
        "UpdateItem",
    )

    result = app.lambda_handler(
        api_event("POST", "/decisions/d-1/execute"), None
    )

    assert result["statusCode"] == 409
    assert "claimed" in body(result)["error"]

    app._test.s3.head_object.assert_not_called()
    app._test.s3.copy_object.assert_not_called()


def test_execute_failed_verification_records_the_mismatch(app):
    """The post-copy HEAD must agree with the approved class; when it
    doesn't (S3 async copy settles later, class drifts, etc.) the
    decision lands in `failed` with the mismatch spelled out."""

    app._test.decisions.get_item.return_value = {
        "Item": approved_decision(app)
    }
    app._test.s3.head_object.side_effect = [
        {"StorageClass": "STANDARD", "ContentLength": 1_000_000_000},
        {"StorageClass": "STANDARD", "ContentLength": 1_000_000_000},
    ]

    result = app.lambda_handler(
        api_event("POST", "/decisions/d-1/execute"), None
    )

    assert result["statusCode"] == 200

    payload = body(result)

    assert payload["decision_state"] == "failed"
    assert "reports storage class" in payload["verification_error"]
    assert "GLACIER_DEEP_ARCHIVE" in payload["verification_error"]

    # The copy itself went out; verification is what refused it.
    app._test.s3.copy_object.assert_called_once()


def test_execute_s3_failure_records_and_surfaces(app):
    """A copy failure becomes `failed` with the S3 error surfaced in
    the response — never a half-claimed saving or a 500."""

    app._test.decisions.get_item.return_value = {
        "Item": approved_decision(app)
    }
    app._test.s3.head_object.side_effect = [
        {"StorageClass": "STANDARD", "ContentLength": 1_000_000},
    ]
    app._test.s3.copy_object.side_effect = RuntimeError("boom")

    result = app.lambda_handler(
        api_event("POST", "/decisions/d-1/execute"), None
    )

    assert result["statusCode"] == 200

    payload = body(result)

    assert payload["decision_state"] == "failed"
    assert "boom" in payload["verification_error"]


def test_execute_refuses_a_non_approved_decision(app):
    for state in ("proposed", "rejected", "verified", "failed"):
        app._test.decisions.get_item.return_value = {
            "Item": {**approved_decision(app), "decision_state": state}
        }

        result = app.lambda_handler(
            api_event("POST", "/decisions/d-1/execute"), None
        )

        assert result["statusCode"] == 409, state

    # The claim update only fires for approved decisions.
    app._test.decisions.update_item.assert_not_called()
    app._test.s3.copy_object.assert_not_called()


def test_execute_accepts_intelligent_tiering_head_quirks(app):
    """S3 may report an IT object's StorageClass as STANDARD; the
    verification accepts both, and every other class must be exact."""

    app._test.decisions.get_item.return_value = {
        "Item": {**approved_decision(app), "to_class": "INTELLIGENT_TIERING"}
    }
    app._test.s3.head_object.side_effect = [
        {"StorageClass": "STANDARD", "ContentLength": 1_000_000_000},
        {"StorageClass": "STANDARD", "ContentLength": 1_000_000_000},
    ]

    result = app.lambda_handler(
        api_event("POST", "/decisions/d-1/execute"), None
    )

    assert result["statusCode"] == 200
    assert body(result)["decision_state"] == "verified"


# ---------------------------------------------------------------
# list / get / ledger
# ---------------------------------------------------------------

def test_list_by_state_uses_the_state_gsi(app):
    app._test.decisions.query.return_value = {"Items": [{"decision_id": "d"}]}

    result = app.lambda_handler(
        api_event("GET", "/decisions", query={"state": "proposed"}), None
    )

    assert result["statusCode"] == 200

    kwargs = app._test.decisions.query.call_args.kwargs

    assert kwargs["IndexName"] == "state-index"
    assert "#s" in kwargs["KeyConditionExpression"]


def test_list_without_state_scans(app):
    app._test.decisions.scan.return_value = {"Items": []}


def test_list_excludes_open_decision_marker_rows(app):
    """Regression for the blank History tab: GET /decisions reads the
    raw table, which also holds the one-open-decision-per-document
    guard markers. Those rows carry no decision fields (and an
    unknown state the UI buckets into History), so the listing must
    filter them out server-side."""

    app._test.decisions.scan.return_value = {
        "Items": [
            {"decision_id": "d-1", "decision_state": "verified"},
            {
                "decision_id": "OPENDEC#doc-9",
                "decision_state": "__open_marker__",
                "document_id": "doc-9",
            },
        ]
    }

    # The FilterExpression is evaluated by DynamoDB server-side; a
    # bare mock must mirror that semantic or the handler under test
    # (which passes it and trusts the result) sees nothing change.
    items = app._test.decisions.scan.return_value["Items"]

    def filtered_scan(**kwargs):
        prefix = kwargs["ExpressionAttributeValues"][":marker_prefix"]
        return {
            "Items": [
                item
                for item in items
                if not item["decision_id"].startswith(prefix)
            ]
        }

    app._test.decisions.scan.side_effect = filtered_scan

    result = app.lambda_handler(api_event("GET", "/decisions"), None)

    payload = body(result)

    assert [d["decision_id"] for d in payload["decisions"]] == ["d-1"]

    # The exclusion rides a FilterExpression on the key prefix.
    scan_kwargs = app._test.decisions.scan.call_args.kwargs
    assert "begins_with(decision_id" in scan_kwargs["FilterExpression"]
    assert (
        scan_kwargs["ExpressionAttributeValues"][":marker_prefix"]
        == "OPENDEC#"
    )

    result = app.lambda_handler(
        api_event("GET", "/decisions"), None
    )

    assert result["statusCode"] == 200
    assert app._test.decisions.query.call_count == 0


def test_get_decision(app):
    app._test.decisions.get_item.return_value = {
        "Item": {"decision_id": "d-2", "decision_state": "proposed"}
    }

    result = app.lambda_handler(api_event("GET", "/decisions/d-2"), None)

    assert result["statusCode"] == 200

    app._test.decisions.get_item.return_value = {"Item": None}

    assert (
        app.lambda_handler(api_event("GET", "/decisions/d-2"), None)[
            "statusCode"
        ]
        == 404
    )


def test_ledger_sums_verified_realized_savings(app):
    app._test.decisions.query.return_value = {
        "Items": [
            {
                "decision_id": "d-1",
                "realized_savings": Decimal("12.5"),
            },
            {
                "decision_id": "d-2",
                "realized_savings": Decimal("0.25"),
            },
            # A malformed row must not take the ledger down.
            {"decision_id": "d-3"},
        ]
    }

    result = app.lambda_handler(api_event("GET", "/ledger"), None)

    assert result["statusCode"] == 200

    payload = body(result)

    assert payload["total_realized_annual_savings"] == pytest.approx(12.75)
    assert "not invoice data" in payload["basis"]

    kwargs = app._test.decisions.query.call_args.kwargs

    assert kwargs["IndexName"] == "state-index"


# ---------------------------------------------------------------
# RBAC — the Approvers group gates the write paths
# ---------------------------------------------------------------

def test_non_approvers_are_403_on_every_write_path(app):
    """A JWT without the Approvers group may read the queue and
    propose single decisions, but approve/reject/execute/batch are
    approver-only."""

    viewer_event = lambda method, path, **kw: api_event(
        method, path, claims={"cognito:groups": "Viewers"}, **kw
    )

    app._test.decisions.get_item.return_value = {
        "Item": proposed_decision(app)
    }

    assert (
        app.lambda_handler(
            viewer_event("POST", "/decisions/d-1/approve"), None
        )["statusCode"]
        == 403
    )
    assert (
        app.lambda_handler(
            viewer_event("POST", "/decisions/d-1/reject"), None
        )["statusCode"]
        == 403
    )

    # The gates fire BEFORE any state write or S3 call.
    app._test.decisions.update_item.assert_not_called()
    app._test.s3.copy_object.assert_not_called()

    app._test.decisions.get_item.return_value = {
        "Item": approved_decision(app)
    }

    assert (
        app.lambda_handler(
            viewer_event("POST", "/decisions/d-1/execute"), None
        )["statusCode"]
        == 403
    )

    app._test.s3.head_object.assert_not_called()

    assert (
        app.lambda_handler(
            viewer_event("POST", "/decisions/propose-batch", body={}),
            None,
        )["statusCode"]
        == 403
    )
    app._test.transact.transact_write_items.assert_not_called()

    # Single-document proposing stays viewer-writable.
    proposed = app.lambda_handler(
        viewer_event("POST", "/decisions", body={"document_id": "doc-9"}),
        None,
    )

    assert proposed["statusCode"] in (201, 409)


def test_approver_claim_allows_the_write_paths(app):
    app._test.decisions.get_item.return_value = {
        "Item": proposed_decision(app)
    }

    # Cognito comma-joins multiple groups into one string.
    approved = app.lambda_handler(
        api_event(
            "POST",
            "/decisions/d-1/approve",
            claims={"cognito:groups": "Approvers,Users"},
        ),
        None,
    )

    assert approved["statusCode"] == 200
    assert body(approved)["decision_state"] == "approved"

    # A raw list shape (other API GW versions) works too.
    app._test.decisions.get_item.return_value = {
        "Item": proposed_decision(app)
    }
    assert (
        app.lambda_handler(
            api_event(
                "POST",
                "/decisions/d-1/approve",
                claims={"cognito:groups": ["Approvers"]},
            ),
            None,
        )["statusCode"]
        == 200
    )


# ---------------------------------------------------------------
# Race guards on the transitions
# ---------------------------------------------------------------

def test_approve_lost_the_state_race_returns_409(app):
    """Two concurrent approvers: exactly one conditional write wins;
    the loser gets the same 409 wording it would see serially."""

    app._test.decisions.get_item.return_value = {
        "Item": proposed_decision(app)
    }
    app._test.decisions.update_item.side_effect = ClientError(
        {
            "Error": {
                "Code": "ConditionalCheckFailedException",
                "Message": "condition failed",
            }
        },
        "UpdateItem",
    )

    result = app.lambda_handler(
        api_event("POST", "/decisions/d-1/approve"), None
    )

    assert result["statusCode"] == 409
    assert "concurrent" in body(result)["error"]


def test_create_lost_the_transaction_race_returns_409(app):
    """Two concurrent proposals of the SAME document: both pass the
    GSI pre-checks; only the marker transaction admits one."""

    wire_cold_fixtures(app)

    cancelled = ClientError(
        {
            "Error": {
                "Code": "TransactionCanceledException",
                "Message": "transaction cancelled",
            },
            "CancellationReasons": [
                {"Code": "ConditionalCheckFailed", "Message": "marker exists"},
                {"Code": "None"},
            ],
        },
        "TransactWriteItems",
    )
    app._test.transact.transact_write_items.side_effect = cancelled

    result = app.lambda_handler(
        api_event("POST", "/decisions", {"document_id": "doc-9"}), None
    )

    assert result["statusCode"] == 409
    assert "already proposed" in body(result)["error"]


def test_propose_batch_includes_open_decision_markers(app):
    """The pending set is GSI states PLUS the marker partition - a
    proposal that won the write race is skipped without relying on
    the decision row being visible yet."""

    app._test.aggregates.scan.return_value = {"Items": [dict(COLD_AGGREGATE)]}
    app._test.documents.scan.return_value = {"Items": [dict(COLD_DOCUMENT)]}

    # query is called once per pending state, in order.
    app._test.decisions.query.side_effect = [
        {"Items": []},  # proposed
        {"Items": []},  # approved
        {"Items": [{"document_id": "doc-9"}]},  # __open_marker__
    ]

    result = app.lambda_handler(
        api_event("POST", "/decisions/propose-batch", {"max": 5}), None
    )

    assert result["statusCode"] == 200

    payload = body(result)

    assert payload["proposed_count"] == 0
    assert payload["skipped"][0]["document_id"] == "doc-9"
    assert payload["skipped"][0]["reason"] == "decision already pending"

    app._test.transact.transact_write_items.assert_not_called()


def test_propose_batch_skips_documents_with_pending_uploads(app):
    """An object whose upload was never confirmed (no HEAD ever ran)
    must not get sized or migrated - the metadata row describes an
    object that may not exist."""

    pending_doc = {**COLD_DOCUMENT, "upload_status": "UPLOAD_PENDING"}

    app._test.aggregates.scan.return_value = {"Items": [dict(COLD_AGGREGATE)]}
    app._test.documents.scan.return_value = {"Items": [pending_doc]}

    app._test.decisions.query.side_effect = [
        {"Items": []},
        {"Items": []},
        {"Items": []},
    ]

    result = app.lambda_handler(
        api_event("POST", "/decisions/propose-batch", {"max": 5}), None
    )

    payload = body(result)

    assert payload["proposed_count"] == 0
    assert payload["skipped"][0]["reason"] == (
        "upload still pending (never confirmed)"
    )

    app._test.transact.transact_write_items.assert_not_called()


def test_propose_batch_skips_a_race_lost_proposal(app):
    """Between the pending snapshot and the transactional write a
    concurrent proposer can win the marker; the loser reports the
    skip instead of raising."""

    app._test.aggregates.scan.return_value = {"Items": [dict(COLD_AGGREGATE)]}
    app._test.documents.scan.return_value = {"Items": [dict(COLD_DOCUMENT)]}

    app._test.decisions.query.side_effect = [
        {"Items": []},
        {"Items": []},
        {"Items": []},
    ]

    app._test.transact.transact_write_items.side_effect = ClientError(
        {
            "Error": {
                "Code": "TransactionCanceledException",
                "Message": "transaction cancelled",
            },
            "CancellationReasons": [
                {"Code": "ConditionalCheckFailed"},
                {"Code": "None"},
            ],
        },
        "TransactWriteItems",
    )

    result = app.lambda_handler(
        api_event("POST", "/decisions/propose-batch", {"max": 5}), None
    )

    assert result["statusCode"] == 200

    payload = body(result)

    assert payload["proposed_count"] == 0
    assert payload["skipped"][0]["reason"] == (
        "decision already proposed (race)"
    )


# ---------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------

def test_list_limit_returns_a_round_trippable_next_token(app):
    """limit=1 serves one GSI page and encodes LastEvaluatedKey; the
    next request must resume from exactly that key."""

    app._test.decisions.query.side_effect = [
        {
            "Items": [{"decision_id": "d-1"}],
            "LastEvaluatedKey": {"decision_id": "d-1"},
        },
        {"Items": [{"decision_id": "d-2"}]},
    ]

    first = app.lambda_handler(
        api_event(
            "GET", "/decisions", query={"limit": "1", "state": "proposed"}
        ),
        None,
    )

    assert first["statusCode"] == 200

    page_one = body(first)

    assert page_one["decisions"] == [{"decision_id": "d-1"}]
    assert page_one["next_token"]

    # The first page's kwargs carried the limit.
    assert app._test.decisions.query.call_args_list[0].kwargs["Limit"] == 1

    token = page_one["next_token"]

    second = app.lambda_handler(
        api_event(
            "GET",
            "/decisions",
            query={
                "limit": "1",
                "state": "proposed",
                "next_token": token,
            },
        ),
        None,
    )

    assert second["statusCode"] == 200

    page_two = body(second)

    assert page_two["decisions"] == [{"decision_id": "d-2"}]
    assert "next_token" not in page_two

    # The continuation key decoded back to the exact LEK.
    continued_kwargs = app._test.decisions.query.call_args.kwargs

    assert continued_kwargs["ExclusiveStartKey"] == {"decision_id": "d-1"}


def test_list_pagination_validation_errors(app):
    result = app.lambda_handler(
        api_event("GET", "/decisions", query={"limit": "0"}), None
    )
    assert result["statusCode"] == 400
    assert "limit must be between 1 and 1000" in body(result)["error"]

    oversize = app.lambda_handler(
        api_event("GET", "/decisions", query={"limit": "1001"}), None
    )
    assert oversize["statusCode"] == 400

    # A token decoding to a non-dict is malformed, not a crash.
    malformed = app.lambda_handler(
        api_event(
            "GET",
            "/decisions",
            query={"next_token": "WzEsMl0="},  # base64 of "[1,2]"
        ),
        None,
    )
    assert malformed["statusCode"] == 400
    assert "next_token" in body(malformed)["error"]


# ---------------------------------------------------------------
# Ledger band totals
# ---------------------------------------------------------------

def test_ledger_band_totals_carry_legacy_rows_in_both_ends(app):
    app._test.decisions.query.return_value = {
        "Items": [
            {
                "decision_id": "d-1",
                "realized_savings": Decimal("12.5"),
                "realized_savings_low": Decimal("10.0"),
                "realized_savings_high": Decimal("14.0"),
            },
            # A legacy pre-band row contributes its point value to
            # both endpoints.
            {"decision_id": "d-2", "realized_savings": Decimal("0.25")},
        ]
    }

    result = app.lambda_handler(api_event("GET", "/ledger"), None)

    assert result["statusCode"] == 200

    payload = body(result)

    assert payload["total_realized_annual_savings"] == pytest.approx(12.75)
    assert payload["total_realized_savings_band"] == pytest.approx(
        [10.25, 14.25]
    )