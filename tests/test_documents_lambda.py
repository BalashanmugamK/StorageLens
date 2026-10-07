"""Handler tests for backend/lambdas/documents/app.py.

The Lambda module builds its boto3 clients at import time, so each
test loads the module through spec_from_file_location with boto3
patched to MagicMocks — every test gets a fresh module and a fresh
set of client/table mocks.

Coverage: route dispatch (including the /download and /access-stats
suffix split), create-body validation, presigned URLs, the access-
event write on download, and the access-stats 30-day roll-up.
"""

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

LAMBDA_PATH = (
    REPO_ROOT
    / "backend"
    / "lambdas"
    / "documents"
    / "app.py"
)


@pytest.fixture()
def app(monkeypatch):
    """Load the documents Lambda with stubbed AWS clients."""

    monkeypatch.setenv("DOCUMENTS_BUCKET", "test-bucket")
    monkeypatch.setenv("DOCUMENTS_TABLE", "DocumentsTable")
    monkeypatch.setenv(
        "ACCESS_HISTORY_TABLE", "AccessHistoryTable"
    )

    s3 = MagicMock()
    dynamodb = MagicMock()
    table = MagicMock()
    history = MagicMock()

    dynamodb.Table.side_effect = lambda name: (
        history if name == "AccessHistoryTable" else table
    )

    with patch(
        "boto3.client", return_value=s3
    ), patch("boto3.resource", return_value=dynamodb):

        spec = importlib.util.spec_from_file_location(
            "documents_app", LAMBDA_PATH
        )

        module = importlib.util.module_from_spec(spec)

        spec.loader.exec_module(module)

    module._test = MagicMock()
    module._test.s3 = s3
    module._test.table = table
    module._test.history = history

    return module


def api_event(method, path, body=None, query=None):
    event = {
        "rawPath": path,
        "requestContext": {"http": {"method": method}},
    }

    if body is not None:
        event["body"] = json.dumps(body)

    if query is not None:
        event["queryStringParameters"] = query

    return event


def body(response):
    return json.loads(response["body"])


def test_route_dispatch(app):
    app._test.table.get_item.return_value = {
        "Item": {
            "document_id": "doc-1",
            "object_key": "documents/doc-1/f.pdf",
            "file_name": "f.pdf",
            "file_size": 10,
            "content_type": "application/pdf",
            "upload_timestamp": "2026-01-01T00:00:00Z",
            "current_storage_class": "STANDARD",
        }
    }

    app._test.table.scan.return_value = {
        "Items": [{"document_id": "doc-1"}]
    }

    app._test.history.query.return_value = {"Items": []}

    assert (
        app.lambda_handler(
            api_event(
                "POST",
                "/documents",
                {
                    "file_name": "f.pdf",
                    "file_size": 10,
                    "content_type": "application/pdf",
                },
            ),
            None,
        )["statusCode"]
        == 201
    )

    assert (
        app.lambda_handler(
            api_event("GET", "/documents"), None
        )["statusCode"]
        == 200
    )

    assert (
        app.lambda_handler(
            api_event("GET", "/documents/doc-1"), None
        )["statusCode"]
        == 200
    )

    assert (
        app.lambda_handler(
            api_event("GET", "/documents/doc-1/download"),
            None,
        )["statusCode"]
        == 200
    )

    assert (
        app.lambda_handler(
            api_event(
                "GET", "/documents/doc-1/access-stats"
            ),
            None,
        )["statusCode"]
        == 200
    )

    assert (
        app.lambda_handler(
            api_event("GET", "/nope"), None
        )["statusCode"]
        == 404
    )

    assert (
        app.lambda_handler(
            api_event("DELETE", "/documents/doc-1"), None
        )["statusCode"]
        == 404
    )


def test_config_route_bootstrap(app):
    """GET /config is the one public route. Without pool env vars it
    degrades to auth: null; with them it hands over the ids."""

    payload = body(
        app.lambda_handler(api_event("GET", "/config"), None)
    )

    assert payload["auth"] is None

    # The module constants are resolved at import time; patch them
    # to simulate a pool-wired deployment.
    with patch.object(
        app,
        "USER_POOL_ID",
        "ap-south-1_TESTPOOL",
        create=True,
    ), patch.object(
        app, "CLIENT_ID", "client123", create=True
    ), patch.object(
        app, "AUTH_DOMAIN", "pool.auth.example", create=True
    ):
        payload = body(
            app.lambda_handler(api_event("GET", "/config"), None)
        )

        assert payload["auth"] == {
            "user_pool_id": "ap-south-1_TESTPOOL",
            "client_id": "client123",
            "auth_domain": "pool.auth.example",
        }


def test_routes_split_on_suffix_not_prefix(app):
    """A GET /documents/doc-1/download must not fall through to
    get_document with document_id 'doc-1/download', and plain
    GET /documents/doc-1 must read access-history table never."""

    app._test.table.get_item.return_value = {"Item": None}

    # The download route queries the history table, not this.
    app.lambda_handler(
        api_event("GET", "/documents/doc-1/download"), None
    )

    # doc exists check happens on the documents table.
    assert (
        app._test.table.get_item.call_count == 1
    )

    app._test.table.get_item.reset_mock()

    result = app.lambda_handler(
        api_event("GET", "/documents/doc-1"), None
    )

    assert result["statusCode"] == 404


def test_create_document_happy_path(app):
    app._test.s3.generate_presigned_url.return_value = (
        "https://presigned.example"
    )

    result = app.lambda_handler(
        api_event(
            "POST",
            "/documents",
            {
                "file_name": "report.pdf",
                "file_size": 1_000_000,
                "content_type": "application/pdf",
            },
        ),
        None,
    )

    assert result["statusCode"] == 201

    payload = body(result)

    assert payload["object_key"] == (
        f"documents/{payload['document_id']}/report.pdf"
    )
    assert payload["upload_url"] == (
        "https://presigned.example"
    )

    item = app._test.table.put_item.call_args.kwargs[
        "Item"
    ]

    assert item["current_storage_class"] == "STANDARD"
    assert item["file_size"] == 1_000_000
    assert item["document_id"] == payload["document_id"]

    # The row starts UPLOAD_PENDING: aggregation and proposals skip
    # it until confirm-upload HEAD-verifies the presigned PUT.
    assert item["upload_status"] == "UPLOAD_PENDING"
    # Claims are absent in this event — the stamp carries None rather
    # than guessing an identity.
    assert "uploaded_by" in item

    presign_kwargs = (
        app._test.s3.generate_presigned_url.call_args.kwargs
    )

    assert presign_kwargs["Params"]["Bucket"] == "test-bucket"
    assert (
        presign_kwargs["Params"]["ContentType"]
        == "application/pdf"
    )
    assert presign_kwargs["ExpiresIn"] == 900


def test_create_document_validations(app):
    bad_bodies = [
        None,  # no body at all
        {},  # empty object
        {
            "file_size": 10,
            "content_type": "text/plain",
        },  # missing file_name
        {"file_name": "", "file_size": 10, "content_type": "text/plain"},
        {
            "file_name": "f",
            "file_size": 0,
            "content_type": "text/plain",
        },
        {
            "file_name": "f",
            "file_size": "big",
            "content_type": "text/plain",
        },
        {"file_name": "f", "file_size": 10, "content_type": "  "},
    ]

    for bad in bad_bodies:
        event = api_event("POST", "/documents", bad)

        if bad is None:
            event["body"] = "{not valid json"

        result = app.lambda_handler(event, None)

        assert result["statusCode"] == 400, bad
        assert "error" in body(result)

    # Nothing was persisted for any rejected request.
    assert app._test.table.put_item.call_count == 0


def test_list_documents(app):
    app._test.table.scan.return_value = {
        "Items": [{"document_id": "a"}, {"document_id": "b"}]
    }

    result = app.lambda_handler(
        api_event("GET", "/documents"), None
    )

    assert result["statusCode"] == 200
    assert len(body(result)["documents"]) == 2


def test_list_documents_limit_pages_with_a_next_token(app):
    """limit=1 serves one scan page; LastEvaluatedKey rides out in a
    URL-safe next_token the next request resumes from exactly."""

    app._test.table.scan.side_effect = [
        {
            "Items": [{"document_id": "a"}],
            "LastEvaluatedKey": {"document_id": "a"},
        },
        {"Items": [{"document_id": "b"}]},
    ]

    first = app.lambda_handler(
        api_event("GET", "/documents", query={"limit": "1"}), None
    )

    assert first["statusCode"] == 200

    page_one = body(first)

    assert page_one["documents"] == [{"document_id": "a"}]
    assert page_one["next_token"]

    assert app._test.table.scan.call_args_list[0].kwargs["Limit"] == 1

    token = page_one["next_token"]

    second = app.lambda_handler(
        api_event("GET", "/documents", query={"limit": "1", "next_token": token}),
        None,
    )

    assert second["statusCode"] == 200

    page_two = body(second)

    assert page_two["documents"] == [{"document_id": "b"}]
    assert "next_token" not in page_two

    assert app._test.table.scan.call_args.kwargs["ExclusiveStartKey"] == {
        "document_id": "a"
    }


def test_list_documents_pagination_validation(app):
    for limit in ("0", "1001"):
        response = app.lambda_handler(
            api_event("GET", "/documents", query={"limit": limit}), None
        )

        assert response["statusCode"] == 400
        assert "limit must be between 1 and 1000" in body(response)["error"]

    malformed = app.lambda_handler(
        api_event(
            "GET", "/documents", query={"next_token": "WzEsMl0="}
        ),
        None,
    )

    assert malformed["statusCode"] == 400
    assert "next_token" in body(malformed)["error"]


def test_download_records_access_event(app):
    app._test.table.get_item.return_value = {
        "Item": {
            "document_id": "doc-2",
            "object_key": "documents/doc-2/f",
        }
    }
    app._test.s3.generate_presigned_url.return_value = (
        "https://dl.example"
    )

    result = app.lambda_handler(
        api_event("GET", "/documents/doc-2/download"), None
    )

    assert result["statusCode"] == 200

    payload = body(result)

    assert payload["download_url"] == "https://dl.example"
    assert payload["access_type"] == "DOWNLOAD"

    event = app._test.history.put_item.call_args.kwargs[
        "Item"
    ]

    assert event["document_id"] == "doc-2"
    assert event["access_type"] == "DOWNLOAD"
    assert "access_timestamp" in event

    presign_params = (
        app._test.s3.generate_presigned_url.call_args.kwargs[
            "Params"
        ]
    )

    assert presign_params["Key"] == "documents/doc-2/f"


def test_download_unknown_document_is_404(app):
    app._test.table.get_item.return_value = {"Item": None}

    result = app.lambda_handler(
        api_event("GET", "/documents/ghost/download"), None
    )

    assert result["statusCode"] == 404

    # No access event for a document that does not exist.
    assert app._test.history.put_item.call_count == 0


def test_access_stats_rollup(app):
    app._test.table.get_item.return_value = {
        "Item": {"document_id": "doc-3"}
    }

    now = datetime.now(timezone.utc)

    events = [
        {"access_timestamp": (now - timedelta(days=1)).isoformat()},
        {"access_timestamp": (now - timedelta(days=10)).isoformat()},
        {"access_timestamp": (now - timedelta(days=45)).isoformat()},
    ]

    app._test.history.query.return_value = {"Items": events}

    result = app.lambda_handler(
        api_event("GET", "/documents/doc-3/access-stats"), None
    )

    assert result["statusCode"] == 200

    payload = body(result)

    assert payload["access_count"] == 3
    assert payload["recent_access_count"] == 2
    assert payload["last_accessed"] == max(
        event["access_timestamp"] for event in events
    )


def test_access_stats_empty_history(app):
    app._test.table.get_item.return_value = {
        "Item": {"document_id": "doc-4"}
    }

    app._test.history.query.return_value = {"Items": []}

    result = app.lambda_handler(
        api_event("GET", "/documents/doc-4/access-stats"), None
    )

    payload = body(result)

    assert result["statusCode"] == 200
    assert payload["access_count"] == 0
    assert payload["last_accessed"] is None
    assert payload["recent_access_count"] == 0


def test_access_stats_unknown_document_is_404(app):
    app._test.table.get_item.return_value = {"Item": None}

    result = app.lambda_handler(
        api_event("GET", "/documents/ghost/access-stats"), None
    )

    assert result["statusCode"] == 404

    assert app._test.history.query.call_count == 0

# ---------------------------------------------------------------
# confirm-upload — the presigned PUT's truth gate
# ---------------------------------------------------------------

CONFIRM_DOC = {
    "document_id": "doc-77",
    "object_key": "documents/doc-77/report.pdf",
    "file_name": "report.pdf",
    "file_size": 1_000,
    "upload_status": "UPLOAD_PENDING",
}


def test_confirm_upload_heads_then_marks_verified(app):
    app._test.table.get_item.return_value = {
        "Item": dict(CONFIRM_DOC)
    }

    result = app.lambda_handler(
        api_event("POST", "/documents/doc-77/confirm-upload"), None
    )

    assert result["statusCode"] == 200

    payload = body(result)

    assert payload["upload_status"] == "UPLOAD_VERIFIED"

    # The object was HEAD-checked before the row was trusted.
    head = app._test.s3.head_object.call_args.kwargs
    assert head["Bucket"] == "test-bucket"
    assert head["Key"] == "documents/doc-77/report.pdf"

    update = app._test.table.update_item.call_args.kwargs

    assert update["ExpressionAttributeValues"][":status"] == (
        "UPLOAD_VERIFIED"
    )
    # The write is conditional: a concurrent confirm cannot double-
    # flip the status past its own check.
    assert "attribute_not_exists(upload_status)" in (
        update["ConditionExpression"]
    )


def test_confirm_upload_missing_object_is_409(app):
    """Abandoned upload: the presigned PUT never happened, so the
    row must NOT become verified - the caller is told to clean up
    and the aggregation skip stays in force."""

    from botocore.exceptions import ClientError

    app._test.table.get_item.return_value = {
        "Item": dict(CONFIRM_DOC)
    }
    app._test.s3.head_object.side_effect = ClientError(
        {"Error": {"Code": "404", "Message": "Not Found"}},
        "HeadObject",
    )

    result = app.lambda_handler(
        api_event("POST", "/documents/doc-77/confirm-upload"), None
    )

    assert result["statusCode"] == 409
    assert "did not complete" in body(result)["error"]
    # Untouched: the row keeps its pending marker.
    app._test.table.update_item.assert_not_called()


def test_confirm_upload_double_confirm_is_a_no_op(app):
    """A row already verified (or a legacy row without the marker)
    confirms idempotently - no writes fire."""

    app._test.table.get_item.return_value = {
        "Item": {**CONFIRM_DOC, "upload_status": "UPLOAD_VERIFIED"}
    }

    result = app.lambda_handler(
        api_event("POST", "/documents/doc-77/confirm-upload"), None
    )

    assert result["statusCode"] == 200
    app._test.s3.head_object.assert_not_called()
    app._test.table.update_item.assert_not_called()

    # A legacy row (no upload_status at all) behaves the same way.
    app._test.table.get_item.return_value = {
        "Item": {"document_id": "doc-77", "object_key": "documents/doc-77/f"}
    }

    assert (
        app.lambda_handler(
            api_event("POST", "/documents/doc-77/confirm-upload"), None
        )["statusCode"]
        == 200
    )
    app._test.s3.head_object.assert_not_called()
    app._test.table.update_item.assert_not_called()


def test_confirm_upload_lost_the_race_returns_409(app):
    from botocore.exceptions import ClientError

    app._test.table.get_item.return_value = {
        "Item": dict(CONFIRM_DOC)
    }
    app._test.table.update_item.side_effect = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException"}},
        "UpdateItem",
    )

    result = app.lambda_handler(
        api_event("POST", "/documents/doc-77/confirm-upload"), None
    )

    assert result["statusCode"] == 409
    assert "already confirmed" in body(result)["error"]


def test_confirm_upload_unknown_document_is_404(app):
    app._test.table.get_item.return_value = {"Item": None}

    result = app.lambda_handler(
        api_event("POST", "/documents/ghost/confirm-upload"), None
    )

    assert result["statusCode"] == 404
    app._test.s3.head_object.assert_not_called()
