"""RECONCILIATION — checking the model against the actual bill.

The cost model makes a claim: migrate this and you save X. The ledger
carries those predicted savings forward after HEAD-verified executions.
Neither is worth anything until someone compares them with what AWS
ACTUALLY charged. This lambda is that comparison:

  ACTUAL    : AWS Cost Explorer UnblendedCost for the Amazon S3
              service in this account, grouped by usage type. Storage
              line items ("TimedStorage-*") are mapped to storage
              classes; everything else (requests, data transfer,
              early delete) is recorded but NOT modeled.
  PREDICTED : the engine's storage-cost component re-run over the
              LIVE aggregate snapshot, converted to a flat per-object
              monthly rate (storage_cost / planning horizon).
  VARIANCE  : predicted - actual, per class and per month.

Both imports above are the same engine bundle the decisions Lambda
ships (pinned byte-identical by tests/test_engine_parity.py) — the
reconciliation never prices with a different model than the
optimizer, ledger and experiments.

Routes (behind the JWT authorizer):
  POST /reconciliation/run      query Cost Explorer, predict, record
  GET  /reconciliation/result   latest recorded report
  GET  /reconciliation/history  recorded reports, newest first
                                (all of them unless `limit` caps the
                                page; default cap 25)
"""

import base64
import json
import logging
import os
import re
from datetime import datetime, timezone
from decimal import Decimal

import boto3

from engine.adapters import from_aggregate_item
from engine.costs import calculate_tier_cost, load_pricing

logger = logging.getLogger(__name__)

# Cost Explorer has regional endpoints; the Lambda runtime's
# AWS_REGION resolves it the same way as every other client here.
ce = boto3.client("ce")

dynamodb = boto3.resource("dynamodb")

AGGREGATES_TABLE = os.environ["AGGREGATES_TABLE"]
DECISIONS_TABLE = os.environ["DECISIONS_TABLE"]
RECONCILIATIONS_TABLE = os.environ["RECONCILIATIONS_TABLE"]

aggregates_table = dynamodb.Table(AGGREGATES_TABLE)
decisions_table = dynamodb.Table(DECISIONS_TABLE)
reconciliations_table = dynamodb.Table(RECONCILIATIONS_TABLE)

# One sort-key family holds every report; GET /reconciliation/result
# reads the newest by running the query backwards.
RECONCILIATION_SCOPE = "reconciliation"

DEFAULT_MONTHS = 3
MAX_MONTHS = 12

# /reconciliation/history serves one page of this size unless the
# caller passes `limit`; without a limit the full history pages out.
HISTORY_DEFAULT_LIMIT = 25

# The bill line the model claims to predict, narrowed to the service.
S3_SERVICE_DIMENSION = "Amazon Simple Storage Service"

# Cost Explorer usage types carry a region/zone prefix
# (APS3-TimedStorage-ByteHrs, USE1-APS3-AWS-Out-Bytes). It is billing
# noise for reconciliation — strip it and match the charge's shape.
_REGION_PREFIX_RE = re.compile(r"^[A-Z]{2,4}\d+-")

# Storage line items map to the model's classes. Anything
# TimedStorage-* that is NOT in this map keeps landing in
# "unmapped_storage" — reported, never dropped — because a silent
# merge would hide spend from a class the model does not price.
_STORAGE_USAGE_TYPE_MAP = {
    "TimedStorage-ByteHrs": "STANDARD",
    "TimedStorage-IA-ByteHrs": "STANDARD_IA",
    "TimedStorage-GIR-ByteHrs": "GLACIER_INSTANT_RETRIEVAL",
    "TimedStorage-GlacierByteHrs": "GLACIER_FLEXIBLE_RETRIEVAL",
    "TimedStorage-DeepArchiveByteHrs": "GLACIER_DEEP_ARCHIVE",
}

# Intelligent-Tiering bills through per-layer line items whose exact
# spelling has varied (…-Int-FA-ByteHrs, …-IntelligentTiering-FA-…);
# they are recognized by the family name instead of enumerated.
_INTELLIGENT_TIERING_RE = re.compile(r"TimedStorage-Int(elligentTiering)?-")


def response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json"
        },
        "body": json.dumps(
            body,
            default=lambda value: float(value)
            if isinstance(value, Decimal)
            else str(value)
        )
    }


def lambda_handler(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method")
    path = event.get("rawPath", "")

    if method == "POST" and path == "/reconciliation/run":
        return run_reconciliation(event)

    if method == "GET" and path == "/reconciliation/result":
        return get_reconciliation_result()

    if method == "GET" and path == "/reconciliation/history":
        return get_reconciliation_history(event)

    return response(404, {"error": "Not found"})


def normalize_usage_type(usage_type):
    """Strip the region/zone prefix from a CE usage type."""

    return _REGION_PREFIX_RE.sub("", usage_type)


def map_usage_type(normalized):
    """One CE usage type -> model class | 'unmapped_storage' |
    'requests_and_transfer'. Storage line items map through
    _STORAGE_USAGE_TYPE_MAP (IT recognized by family name)."""

    if _INTELLIGENT_TIERING_RE.search(normalized):
        return "INTELLIGENT_TIERING"

    if normalized in _STORAGE_USAGE_TYPE_MAP:
        return _STORAGE_USAGE_TYPE_MAP[normalized]

    if normalized.startswith("TimedStorage"):
        return "unmapped_storage"

    # Requests, data transfer, early-deletion penalties and any
    # other S3 line item: real charges, but outside the model.
    return "requests_and_transfer"


def actual_s3_charges_by_month(months):
    """{month_start: {bucket: usd}} from Cost Explorer, plus whether
    the last month is still an estimate. Storage line items land on
    their class; unmapped TimedStorage items and every non-storage
    charge are kept in their own buckets."""

    window = complete_month_window(months)

    ce_result = ce.get_cost_and_usage(
        TimePeriod={
            "Start": window["start"],
            "End": window["end"],
        },
        Granularity="MONTHLY",
        Metrics=["UnblendedCost"],
        Filter={
            "Dimensions": {
                "Key": "SERVICE",
                "Values": [S3_SERVICE_DIMENSION],
            }
        },
        GroupBy=[{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
    )

    monthly = {}
    estimated = []

    for result in ce_result.get("ResultsByTime", []):
        month = result["TimePeriod"]["Start"]

        if result.get("Estimated"):
            estimated.append(month)

        charges = monthly.setdefault(month, {})

        for group in result.get("Groups", []):
            bucket = map_usage_type(
                normalize_usage_type(group["Keys"][0])
            )
            amount = float(
                group["Metrics"]["UnblendedCost"]["Amount"]
            )
            charges[bucket] = (
                charges.get(bucket, 0.0) + amount
            )

    return monthly, estimated


def complete_month_window(months):
    """First-into-last day strings for the `months` COMPLETE months
    before this one. Cost Explorer's current partial month is
    estimated and always excluded."""

    today = datetime.now(timezone.utc)
    first_of_current = today.replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )

    def previous_first(anchor):
        year, month = (
            (anchor.year - 1, 12) if anchor.month == 1
            else (anchor.year, anchor.month - 1)
        )
        return anchor.replace(year=year, month=month)

    first = first_of_current
    for _ in range(months):
        first = previous_first(first)

    def to_iso(dt):
        return dt.strftime("%Y-%m-%d")

    return {"start": to_iso(first), "end": to_iso(first_of_current)}


def predicted_monthly_by_class(pricing):
    """{class: usd} — the engine's storage component re-run over the
    LIVE aggregate snapshot, as a flat per-object monthly rate.

    Each document's 12-month storage_cost (minimum billable object
    size, archive metadata overhead, Intelligent-Tiering's Poisson
    layer blend and its monitoring-free sub-128KB case are all the
    engine's own handling, not a here-written approximation) is
    divided back out by the planning horizon. Returns (totals,
    sample_errors, failure_count)."""

    horizon = pricing["model_assumptions"]["planning_horizon_months"]

    totals = {}
    errors = []
    failures = 0

    for item in scan_all(aggregates_table):
        try:
            document = from_aggregate_item(item)
            breakdown = calculate_tier_cost(
                document,
                document.current_storage_class,
                include_transition=False,
            )
            monthly = float(breakdown.storage_cost) / horizon
        except Exception as exc:  # one bad aggregate must not kill the run
            failures += 1

            if len(errors) < 5:
                errors.append(
                    {
                        "document_id": item.get("document_id"),
                        "error": str(exc),
                    }
                )
            continue

        cls = document.current_storage_class
        totals[cls] = totals.get(cls, 0.0) + monthly

    return totals, errors, failures


def scan_all(table):
    """Every item of a table, paged (a bare scan() stops at ~1 MB)."""

    items = []
    kwargs = {}

    while True:
        page = table.scan(**kwargs)
        items.extend(page.get("Items", []))
        last = page.get("LastEvaluatedKey")

        if not last:
            return items

        kwargs["ExclusiveStartKey"] = last


def verified_ledger_summary():
    """Realized-savings context from the decisions table: what the
    APPROVAL side claims it was worth so far. Attribution of a bill
    delta to individual transitions is NOT attempted — the report
    keeps the two numbers side by side and the basis says why."""

    summary = {"verified_decisions": 0, "realized_savings_annual": 0.0}

    for decision in scan_all(decisions_table):
        if decision.get("decision_state") == "verified":
            summary["verified_decisions"] += 1
            summary["realized_savings_annual"] += float(
                decision.get("realized_savings", "0") or "0"
            )

    return summary


def to_dynamo(value):
    """DynamoDB rejects floats; the report carries money, so Decimal
    applies recursively (nested months dicts included)."""

    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {key: to_dynamo(item) for key, item in value.items()}
    if isinstance(value, list):
        return [to_dynamo(item) for item in value]
    return value


def build_report(months):
    """Run one reconciliation: bill lines, engine predictions,
    variance per class, the ledger's claim so far."""

    pricing = load_pricing()

    actual_monthly, estimated = actual_s3_charges_by_month(months)
    predicted_by_class, prediction_errors, prediction_failures = (
        predicted_monthly_by_class(pricing)
    )

    # Only classes the model prices take part in the comparison; an
    # unmodelled class would silently predict nothing and look like a
    # clean zero-variance match.
    predicted = {
        cls: amount
        for cls, amount in predicted_by_class.items()
        if cls in pricing["storage_classes"]
    }

    report_months = []

    for month in sorted(actual_monthly):
        actual = actual_monthly[month]

        variance = {
            # unmapped_storage stays visible: predicted 0 against real
            # spend is exactly what the reconciliation must expose.
            bucket: round(
                predicted.get(bucket, 0.0) - amount, 6
            )
            for bucket, amount in actual.items()
            if bucket != "requests_and_transfer"
        }
        # A class the model predicts but this month's bill never
        # charged still belongs in the comparison (actual 0).
        for cls, amount in predicted.items():
            if cls not in variance:
                variance[cls] = round(amount, 6)

        actual_non_model = round(
            actual.get("requests_and_transfer", 0.0)
            + actual.get("unmapped_storage", 0.0),
            6,
        )

        report_months.append(
            {
                "month": month,
                "actual_usd": _round_dict(actual),
                "predicted_usd": _round_dict(
                    {
                        cls: amount
                        for cls, amount in predicted.items()
                        if cls != "unmapped_storage"
                    }
                ),
                "variance_usd": variance,
                "actual_non_model_usd": actual_non_model,
                "estimated_month": month in estimated,
            }
        )

    predicted_total = round(sum(predicted.values()), 6)
    actual_total = round(
        sum(
            amount
            for month in actual_monthly.values()
            for key, amount in month.items()
            if key not in ("requests_and_transfer", "unmapped_storage")
        ),
        6,
    )

    ledger = verified_ledger_summary()

    return {
        "scope": RECONCILIATION_SCOPE,
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "months_requested": months,
        "months": report_months,
        "totals": {
            "predicted_storage_usd": predicted_total,
            "actual_storage_usd": actual_total,
            "variance_usd": round(
                predicted_total - actual_total, 6
            ),
            "actual_non_storage_usd": round(
                sum(
                    month.get("requests_and_transfer", 0.0)
                    + month.get("unmapped_storage", 0.0)
                    for month in actual_monthly.values()
                ),
                6,
            ),
            "months_estimated": estimated,
        },
        "ledger": ledger,
        "prediction_errors": prediction_errors,
        "prediction_failures": prediction_failures,
        "basis": (
            "Actual charges are AWS Cost Explorer UnblendedCost for "
            "the Amazon S3 service in this account, grouped by usage "
            "type: TimedStorage line items map to storage classes, "
            "every other charge (requests, transfer, early delete) is "
            "reported as non-model and never predicted. Predicted "
            "charges re-run the engine's storage component over the "
            "LIVE aggregate snapshot as a flat monthly rate — fleet "
            "composition drift within a month is not modeled. "
            "Variance = predicted - actual (positive: model expects "
            "more spend than the bill shows)."
        ),
    }


def _round_dict(mapping):
    return {key: round(value, 6) for key, value in mapping.items()}


def run_reconciliation(event):
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return response(400, {"error": "Request body must be valid JSON"})

    months = body.get("months", DEFAULT_MONTHS)

    if not isinstance(months, int) or isinstance(months, bool):
        return response(400, {"error": "months must be an integer"})
    if months < 1 or months > MAX_MONTHS:
        return response(
            400,
            {
                "error": f"months must be between 1 and {MAX_MONTHS}"
            },
        )

    try:
        report = build_report(months)
    except Exception as exc:
        logger.exception("reconciliation run failed")
        return response(
            502,
            {
                "error": (
                    "Reconciliation failed against the live billing "
                    f"data: {exc}"
                )
            },
        )

    try:
        reconciliations_table.put_item(Item=to_dynamo(report))
    except Exception:
        logger.exception("failed to record the reconciliation report")
        return response(
            500,
            {"error": "Failed to record the reconciliation report"},
        )

    return response(200, {key: value for key, value in report.items() if key != "scope"})


def get_reconciliation_result():
    try:
        result = reconciliations_table.query(
            # "scope" is a DynamoDB reserved keyword — it must go
            # through an ExpressionAttributeName placeholder.
            KeyConditionExpression="#scope = :scope",
            ExpressionAttributeNames={"#scope": "scope"},
            ExpressionAttributeValues={":scope": RECONCILIATION_SCOPE},
            ScanIndexForward=False,
            Limit=1,
        )
    except Exception:
        logger.exception("failed to read the latest reconciliation")
        return response(
            500,
            {"error": "Failed to read the reconciliation report"},
        )

    items = result.get("Items", [])

    if not items:
        return response(
            404,
            {
                "error": (
                    "No reconciliation has run yet — use "
                    "POST /reconciliation/run"
                )
            },
        )

    return response(200, drop_scope(items[0]))


def get_reconciliation_history(event=None):
    """GET /reconciliation/history — the recorded reports, newest
    first. A bare query() returns only ONE page (~1 MB), so the route
    pages through everything unless the caller passes `limit`
    (default 25), in which case ONE page of that size is served and
    LastEvaluatedKey rides out in `next_token`."""

    query = (event or {}).get("queryStringParameters") or {}

    try:
        limit_raw = query.get("limit")

        if limit_raw in (None, ""):
            limit = HISTORY_DEFAULT_LIMIT
        else:
            limit = int(limit_raw)

            if not (1 <= limit <= 1000):
                raise ValueError("limit must be between 1 and 1000")
    except ValueError as error:
        return response(400, {"error": str(error)})

    token = query.get("next_token")

    if token in (None, ""):
        start_key = None
    else:
        try:
            start_key = json.loads(
                base64.urlsafe_b64decode(token.encode()).decode()
            )
        except ValueError:
            return response(400, {"error": "next_token is malformed"})

        if not isinstance(start_key, dict):
            return response(400, {"error": "next_token is malformed"})

    try:
        history = []
        query_kwargs = {}

        if start_key:
            query_kwargs["ExclusiveStartKey"] = start_key

        while True:
            query_kwargs.setdefault("Limit", limit)

            result = reconciliations_table.query(
                KeyConditionExpression="#scope = :scope",
                ExpressionAttributeNames={"#scope": "scope"},
                ExpressionAttributeValues={":scope": RECONCILIATION_SCOPE},
                ScanIndexForward=False,
                **query_kwargs,
            )

            history.extend(
                drop_scope(item) for item in result.get("Items", [])
            )

            last = result.get("LastEvaluatedKey")

            if not last:
                break

            query_kwargs["ExclusiveStartKey"] = last

            if limit and len(history) >= limit:
                break
    except Exception:
        logger.exception("failed to read reconciliation history")
        return response(
            500,
            {"error": "Failed to read reconciliation history"},
        )

    body = {"history": history, "basis": (
        "The reports themselves carry their basis strings; each row "
        "is one reconciliation run in full."
    )}

    if limit and history and last:
        body["next_token"] = base64.urlsafe_b64encode(
            json.dumps(last).encode()
        ).decode()

    return response(200, body)


def drop_scope(item):
    return {key: value for key, value in item.items() if key != "scope"}