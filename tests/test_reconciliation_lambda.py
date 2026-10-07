"""Handler tests for backend/lambdas/reconciliation/app.py.

The reconciliation Lambda is the honesty check: Cost Explorer's actual
S3 charges for complete months against the engine's predicted storage
cost over the live aggregate snapshot, variance per class.

The wiring (routing, month window bounds, variance direction, ledger
context, report recording) is what is under test; the cost model
itself is the pinned optimization/ package.
"""

import importlib.util
import json
import re
import sys
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(REPO_ROOT))

from optimization.adapters import from_aggregate_item
from optimization.costs import calculate_tier_cost, load_pricing

RECONCILIATION_LAMBDA_DIR = (
    REPO_ROOT / "backend" / "lambdas" / "reconciliation"
)
LAMBDA_PATH = RECONCILIATION_LAMBDA_DIR / "app.py"


@pytest.fixture()
def app(monkeypatch):
    """Load the reconciliation Lambda with stubbed AWS clients."""

    monkeypatch.setenv("AGGREGATES_TABLE", "AggregatesTable")
    monkeypatch.setenv("DECISIONS_TABLE", "DecisionsTable")
    monkeypatch.setenv("RECONCILIATIONS_TABLE", "ReconciliationsTable")

    ce_client = MagicMock()
    dynamodb = MagicMock()
    aggregates = MagicMock()
    decisions = MagicMock()
    reconciliations = MagicMock()

    dynamodb.Table.side_effect = lambda name: {
        "AggregatesTable": aggregates,
        "DecisionsTable": decisions,
        "ReconciliationsTable": reconciliations,
    }[name]

    ce_client.get_cost_and_usage.return_value = {
        "ResultsByTime": []
    }

    clients = {"ce": ce_client}

    with patch(
        "boto3.client",
        side_effect=lambda name, **kwargs: clients[name],
    ), patch("boto3.resource", return_value=dynamodb):

        spec = importlib.util.spec_from_file_location(
            "reconciliation",
            LAMBDA_PATH,
            submodule_search_locations=[str(RECONCILIATION_LAMBDA_DIR)],
        )

        module = importlib.util.module_from_spec(spec)

        sys.modules["reconciliation"] = module

        # Mirror the Lambda runtime's sys.path: the CodeUri dir is
        # the task root, so the vendored engine imports by absolute
        # package name.
        sys.path.insert(0, str(RECONCILIATION_LAMBDA_DIR))
        spec.loader.exec_module(module)

    module._test = MagicMock()
    module._test.ce = ce_client
    module._test.aggregates = aggregates
    module._test.decisions = decisions
    module._test.reconciliations = reconciliations

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


def body_of(result):
    return json.loads(result["body"])


# The predicted side is computed against the repo's own package,
# never hardcoded: these two aggregates cover a standard-tier doc
# and an IA small object (minimum-billable-size path).
AGGREGATES = [
    {
        "document_id": "cold-doc",
        "file_size_bytes": 10_000_000_000,
        "current_storage_class": "STANDARD",
        "upload_timestamp": "2026-01-01T00:00:00Z",
        "last_accessed_timestamp": "2026-02-01T00:00:00Z",
        "access_count": 0,
        "days_since_last_access": 240.0,
        "access_frequency": 0.0,
        "aggregation_timestamp": "2026-04-01T00:00:00Z",
        "document_state": "ARCHIVED",
    },
    {
        "document_id": "small-doc",
        "file_size_bytes": 1024,
        "current_storage_class": "STANDARD_IA",
        "upload_timestamp": "2026-01-01T00:00:00Z",
        "access_count": 4,
        "days_since_last_access": 3.0,
        "access_frequency": 0.1,
        "aggregation_timestamp": "2026-04-01T00:00:00Z",
        "document_state": None,
    },
]


def expected_predicted_monthly():
    """What the model says these aggregates cost in storage, per
    month, computed the exact way the lambda computes it."""

    pricing = load_pricing()
    horizon = pricing["model_assumptions"]["planning_horizon_months"]

    totals = {}
    for item in AGGREGATES:
        document = from_aggregate_item(item)
        breakdown = calculate_tier_cost(
            document,
            document.current_storage_class,
            include_transition=False,
        )
        totals[document.current_storage_class] = (
            totals.get(document.current_storage_class, 0.0)
            + float(breakdown.storage_cost) / horizon
        )
    return totals


def stub_ce_response(app, months_charges):
    """months_charges: [(month, [(usage_type, amount)...])]
    — one ResultsByTime entry per month."""

    results = []
    for month, groups in months_charges:
        results.append(
            {
                "TimePeriod": {"Start": month},
                "Estimated": False,
                "Groups": [
                    {
                        "Keys": [usage_type],
                        "Metrics": {"UnblendedCost": {"Amount": str(amount)}},
                    }
                    for usage_type, amount in groups
                ],
            }
        )

    app._test.ce.get_cost_and_usage.return_value = {
        "ResultsByTime": results
    }


# ---------------------------------------------------------------
# Usage-type mapping
# ---------------------------------------------------------------


def test_region_prefix_is_stripped_and_storage_types_map(app):
    m = app.normalize_usage_type
    map_usage_type = app.map_usage_type

    assert m("APS3-TimedStorage-ByteHrs") == "TimedStorage-ByteHrs"
    assert m("USE1-APS3-AWS-Out-Bytes") == "APS3-AWS-Out-Bytes"

    assert map_usage_type("TimedStorage-ByteHrs") == "STANDARD"
    assert map_usage_type("TimedStorage-IA-ByteHrs") == "STANDARD_IA"
    assert (
        map_usage_type("TimedStorage-GIR-ByteHrs")
        == "GLACIER_INSTANT_RETRIEVAL"
    )
    assert (
        map_usage_type("TimedStorage-GlacierByteHrs")
        == "GLACIER_FLEXIBLE_RETRIEVAL"
    )
    assert (
        map_usage_type("TimedStorage-DeepArchiveByteHrs")
        == "GLACIER_DEEP_ARCHIVE"
    )


def test_intelligent_tiering_line_items_match_by_family(app):
    # Both observed spellings of the per-layer line items.
    assert (
        app.map_usage_type(app.normalize_usage_type(
            "APS3-TimedStorage-Int-FA-ByteHrs"
        ))
        == "INTELLIGENT_TIERING"
    )
    assert (
        app.map_usage_type(app.normalize_usage_type(
            "USW2-TimedStorage-IntelligentTiering-IA-ByteHrs"
        ))
        == "INTELLIGENT_TIERING"
    )


def test_unknown_timedstorage_is_reported_not_merged(app):
    """One Zone-IA (and any future storage line item) must stay
    visible as its own bucket — never folded into a modelled class."""

    assert (
        app.map_usage_type("TimedStorage-ZIA-ByteHrs")
        == "unmapped_storage"
    )


def test_non_storage_charges_stay_non_model(app):
    for usage_type in (
        "APS3-Requests-Tier1",
        "USE1-APS3-AWS-Out-Bytes",
        "APS3-EarlyDelete-ByteHrs",
        "USE1-APS3-DeltaOut-Bytes",
    ):
        assert (
            app.map_usage_type(app.normalize_usage_type(usage_type))
            == "requests_and_transfer"
        ), usage_type


# ---------------------------------------------------------------
# The run
# ---------------------------------------------------------------


def test_run_computes_variance_per_month_and_records(app):
    stub_ce_response(
        app,
        [
            (
                "2026-08-01",
                [
                    ("APS3-TimedStorage-ByteHrs", "0.05"),
                    ("APS3-Requests-Tier1", "0.01"),
                ],
            ),
            (
                "2026-09-01",
                [
                    ("APS3-TimedStorage-ByteHrs", "0.04"),
                    ("APS3-TimedStorage-IA-ByteHrs", "0.001"),
                ],
            ),
        ],
    )
    app._test.aggregates.scan.return_value = {"Items": AGGREGATES}
    # One verified decision on the books.
    app._test.decisions.scan.return_value = {
        "Items": [
            {"decision_id": "d1", "decision_state": "verified",
             "realized_savings": Decimal("1.234")},
            {"decision_id": "d2", "decision_state": "proposed",
             "realized_savings": Decimal("99")},
        ]
    }

    result = app.lambda_handler(
        api_event("POST", "/reconciliation/run", {}), None
    )

    assert result["statusCode"] == 200
    report = json.loads(result["body"])

    expected = expected_predicted_monthly()

    # Standard months: predicted - actual, variance direction
    # (positive means the MODEL expects more spend than the bill).
    august = next(
        m for m in report["months"] if m["month"] == "2026-08-01"
    )
    assert august["actual_usd"]["STANDARD"] == pytest.approx(
        0.05, abs=1e-9
    )
    assert august["predicted_usd"]["STANDARD"] == pytest.approx(
        expected["STANDARD"], abs=1e-6
    )
    assert august["variance_usd"]["STANDARD"] == pytest.approx(
        expected["STANDARD"] - 0.05, abs=1e-6
    )
    # Requests are real charges outside the model — recorded, never
    # predicted, never folded into storage totals.
    assert august["actual_usd"]["requests_and_transfer"] == 0.01
    assert "requests_and_transfer" not in august["predicted_usd"]
    assert august["actual_non_model_usd"] == 0.01

    # September exercises both storage classes at once.
    september = next(
        m for m in report["months"] if m["month"] == "2026-09-01"
    )
    assert september["actual_usd"]["STANDARD_IA"] == pytest.approx(
        0.001, abs=1e-9
    )
    assert september["predicted_usd"]["STANDARD_IA"] == pytest.approx(
        expected["STANDARD_IA"], abs=1e-6
    )

    # Totals: only storage line items, no requests/transfer.
    assert report["totals"]["predicted_storage_usd"] == pytest.approx(
        sum(expected.values()), abs=1e-6
    )
    assert report["totals"]["actual_storage_usd"] == pytest.approx(
        0.091, abs=1e-9
    )
    assert report["totals"]["actual_non_storage_usd"] == pytest.approx(
        0.01, abs=1e-9
    )

    # The ledger's claim rides along as context.
    assert report["ledger"]["verified_decisions"] == 1
    assert report["ledger"]["realized_savings_annual"] == pytest.approx(
        1.234, abs=1e-9
    )

    # The report is recorded with the scope/sort-key family intact
    # (the response drops the scope, the stored item keeps it).
    stored = app._test.reconciliations.put_item.call_args.kwargs["Item"]
    assert stored["scope"] == "reconciliation"
    assert "ran_at" in stored
    assert report["ran_at"] == stored["ran_at"]

    # And the basis string names both sources' limits.
    assert "Cost Explorer" in report["basis"]
    assert "not modeled" in report["basis"]


def test_run_validates_the_month_count(app):
    result = app.lambda_handler(
        api_event("POST", "/reconciliation/run", {"months": 13}), None
    )
    assert result["statusCode"] == 400

    result = app.lambda_handler(
        api_event("POST", "/reconciliation/run", {"months": 0}), None
    )
    assert result["statusCode"] == 400

    result = app.lambda_handler(
        api_event("POST", "/reconciliation/run", {"months": "3"}), None
    )
    assert result["statusCode"] == 400

    app._test.ce.get_cost_and_usage.assert_not_called()


def test_run_defaults_to_three_months(app):
    app._test.aggregates.scan.return_value = {"Items": []}
    app._test.decisions.scan.return_value = {"Items": []}

    app.lambda_handler(
        api_event("POST", "/reconciliation/run", {}), None
    )

    period = app._test.ce.get_cost_and_usage.call_args.kwargs["TimePeriod"]

    # The window must END at the current month start and START three
    # complete months earlier.
    assert period["End"].endswith("-01")
    start = period["Start"]
    match = re.fullmatch(r"(\d{4})-(\d{2})-01", start)
    assert match, f"{start} is not a first-of-month boundary"


def test_run_survives_a_billing_api_failure_honestly(app):
    app._test.ce.get_cost_and_usage.side_effect = Exception(
        "TooManyRequestsException"
    )

    result = app.lambda_handler(
        api_event("POST", "/reconciliation/run", {}), None
    )

    assert result["statusCode"] == 502
    body = json.loads(result["body"])
    assert "TooManyRequestsException" in body["error"]
    # Nothing is recorded from a failed comparison.
    app._test.reconciliations.put_item.assert_not_called()


def test_run_tolerates_a_bad_aggregate(app):
    stub_ce_response(
        app,
        [("2026-09-01", [("APS3-TimedStorage-ByteHrs", "0.1")])],
    )

    broken = {"document_id": "bad-doc", "file_size_bytes": "NaN"}  # missing fields
    app._test.aggregates.scan.return_value = {
        "Items": [broken] + AGGREGATES
    }
    app._test.decisions.scan.return_value = {"Items": []}

    result = app.lambda_handler(
        api_event("POST", "/reconciliation/run", {}), None
    )

    assert result["statusCode"] == 200
    report = json.loads(result["body"])

    # Reported honestly, capped: a sample plus the count.
    assert report["prediction_failures"] == 1
    assert len(report["prediction_errors"]) == 1
    assert report["prediction_errors"][0]["document_id"] == "bad-doc"


# ---------------------------------------------------------------
# Reading reports back
# ---------------------------------------------------------------


def test_result_returns_latest_and_drops_scope(app):
    app._test.reconciliations.query.return_value = {
        "Items": [
            {"scope": "reconciliation", "ran_at": "2026-10-07T00:00:00+00:00",
             "totals": {"variance_usd": 0.1}},
        ]
    }

    result = app.lambda_handler(
        api_event("GET", "/reconciliation/result"), None
    )

    assert result["statusCode"] == 200
    report = json.loads(result["body"])
    assert "scope" not in report
    assert report["ran_at"] == "2026-10-07T00:00:00+00:00"

    # The newest is a one-element query run backwards on sort key.
    query = app._test.reconciliations.query.call_args.kwargs
    assert query["ScanIndexForward"] is False
    assert query["Limit"] == 1
    # "scope" is a DynamoDB reserved keyword — the condition must
    # route it through an ExpressionAttributeName placeholder (a bare
    # KeyConditionExpression fails validation against real DynamoDB).
    assert query["KeyConditionExpression"] == "#scope = :scope"
    assert query["ExpressionAttributeNames"] == {"#scope": "scope"}


def test_result_404s_before_the_first_run(app):
    app._test.reconciliations.query.return_value = {"Items": []}

    result = app.lambda_handler(
        api_event("GET", "/reconciliation/result"), None
    )

    assert result["statusCode"] == 404


def test_history_lists_runs_descending_and_drops_scope(app):
    app._test.reconciliations.query.return_value = {
        "Items": [
            {"scope": "reconciliation", "ran_at": f"2026-10-0{i}T00:00:00+00:00"}
            for i in (2, 1)
        ]
    }

    result = app.lambda_handler(
        api_event("GET", "/reconciliation/history"), None
    )

    assert result["statusCode"] == 200
    history = json.loads(result["body"])["history"]
    assert [row["ran_at"][:10] for row in history] == [
        "2026-10-02", "2026-10-01"
    ]
    assert all("scope" not in row for row in history)

    query = app._test.reconciliations.query.call_args.kwargs
    assert query["KeyConditionExpression"] == "#scope = :scope"
    assert query["ExpressionAttributeNames"] == {"#scope": "scope"}
    # Without a client limit the route still pages the FULL history
    # server-side, but defaults the per-page cap to 25.
    assert query["Limit"] == 25


def test_history_limit_serves_one_page_with_next_token(app):
    app._test.reconciliations.query.side_effect = [
        {
            "Items": [
                {"scope": "reconciliation", "ran_at": "2026-10-02T00:00:00+00:00"}
            ],
            "LastEvaluatedKey": {"scope": "reconciliation", "ran_at": "2026-10-02T00:00:00+00:00"},
        },
        {"Items": []},
    ]

    result = app.lambda_handler(
        api_event("GET", "/reconciliation/history", query={"limit": "1"}),
        None,
    )

    assert result["statusCode"] == 200

    payload = json.loads(result["body"])

    assert len(payload["history"]) == 1
    assert payload["next_token"]

    assert app._test.reconciliations.query.call_args_list[0].kwargs["Limit"] == 1

    follow = app.lambda_handler(
        api_event(
            "GET",
            "/reconciliation/history",
            query={"limit": "1", "next_token": payload["next_token"]},
        ),
        None,
    )

    assert follow["statusCode"] == 200

    follow_payload = json.loads(follow["body"])

    assert follow_payload["history"] == []
    assert "next_token" not in follow_payload

    continued = app._test.reconciliations.query.call_args.kwargs

    assert continued["ExclusiveStartKey"]["ran_at"] == (
        "2026-10-02T00:00:00+00:00"
    )


def test_history_limit_validation(app):
    for limit in ("0", "1001", "NaN"):
        result = app.lambda_handler(
            api_event("GET", "/reconciliation/history", query={"limit": limit}),
            None,
        )

        assert result["statusCode"] == 400


def test_unknown_routes_404(app):
    assert app.lambda_handler(
        api_event("GET", "/reconciliation/run"), None
    )["statusCode"] == 404

    assert app.lambda_handler(
        api_event("POST", "/reconciliation/result", {}), None
    )["statusCode"] == 404

    assert app.lambda_handler(
        api_event("GET", "/reconciliation/nope"), None
    )["statusCode"] == 404