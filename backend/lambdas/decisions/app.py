import base64
import json
import logging
import math
import os
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

# Absolute imports: the Lambda runtime loads this file as a bare
# top-level `app` module (Handler "app.lambda_handler"), which has no
# parent package — a relative `from .engine...` would raise
# ImportModuleError on every invocation.
from engine.adapters import from_aggregate_item
from engine.confidence import poisson_interval
from engine.constraints import STATE_ELIGIBILITY
from engine.costs import calculate_billable_size_gb, load_pricing
from engine.optimizer import (
    POLICY_A,
    POLICY_B,
    optimize_document,
    savings_band,
)

logger = logging.getLogger(__name__)

# Region comes from the Lambda runtime's AWS_REGION environment
# variable (resolved by boto3), so the deployment works in any
# region without a hard-coded regional endpoint.
s3 = boto3.client("s3")

dynamodb = boto3.resource("dynamodb")

DOCUMENTS_BUCKET = os.environ["DOCUMENTS_BUCKET"]
DOCUMENTS_TABLE = os.environ["DOCUMENTS_TABLE"]
AGGREGATES_TABLE = os.environ["AGGREGATES_TABLE"]
DECISIONS_TABLE = os.environ["DECISIONS_TABLE"]

documents_table = dynamodb.Table(DOCUMENTS_TABLE)
aggregates_table = dynamodb.Table(AGGREGATES_TABLE)
decisions_table = dynamodb.Table(DECISIONS_TABLE)

# Decision lifecycle: proposed -> approved -> verified | failed,
# with rejected leaving proposed. The state string is stored
# verbatim; the state-index GSI partitions the approval queue on
# it.
STATE_PROPOSED = "proposed"
STATE_APPROVED = "approved"
STATE_REJECTED = "rejected"
STATE_VERIFIED = "verified"
STATE_FAILED = "failed"

# Batch proposals are capped so one click can neither run the engine
# over an unbounded fleet nor write an unbounded number of records;
# repeat the call to keep proposing while aggregates remain.
DEFAULT_PROPOSE_BATCH = 25
MAX_PROPOSE_BATCH = 100

# HeadObject may report Intelligent-Tiering objects as their class
# plainly or as STANDARD depending on API version; the sub-tiers
# (FA/IA/AIA) are internal and never appear. Every other target
# class must come back from the HEAD byte-exact.
ACCEPTED_HEAD_CLASSES = {
    "INTELLIGENT_TIERING": {"INTELLIGENT_TIERING", "STANDARD"},
}

# The engine prices decisions and records storage tiers by AWS's
# *billing* names (the keys of optimization/pricing.json), but the
# S3 API speaks its own *enum* names for CopyObject /
# CreateMultipartUpload, and HeadObject reports the enum back. Both
# directions pass through this mapping at the execution boundary so
# decision records, the verification check and the documents table
# keep engine names and stay consistent with the aggregates.
S3_API_CLASS_BY_ENGINE_NAME = {
    "GLACIER_INSTANT_RETRIEVAL": "GLACIER_IR",
    "GLACIER_FLEXIBLE_RETRIEVAL": "GLACIER",
    "GLACIER_DEEP_ARCHIVE": "DEEP_ARCHIVE",
}
ENGINE_CLASS_BY_S3_API_NAME = {
    api_name: engine_name
    for engine_name, api_name in S3_API_CLASS_BY_ENGINE_NAME.items()
}

# Flexible/deep archive objects are offline: S3 refuses CopyObject
# from an archived object until a restore completes, so re-tiering
# OUT of them is not executable this way. GLACIER_IR keeps instant
# access and copies out freely. Engine billing names for the
# propose-side skip, API enum names for the execute-side check.
RESTORE_REQUIRED_CLASSES = {
    "GLACIER_FLEXIBLE_RETRIEVAL",
    "GLACIER_DEEP_ARCHIVE",
}
RESTORE_REQUIRED_API_CLASSES = {"GLACIER", "DEEP_ARCHIVE"}

# RBAC: only members of this Cognito user-pool group may approve,
# reject, bulk-propose or execute. Reads (queue, ledger, projection)
# stay open to any signed-in user.
APPROVER_GROUP = "Approvers"

# One open decision per document is enforced by a marker item whose
# primary key is deterministic per document; because key existence
# can be used in a ConditionExpression (GSI attributes cannot), a
# transaction putting decision + marker is the race-proof guard.
# uuid4 decision ids can never collide with this prefix.
OPEN_DECISION_PREFIX = "OPENDEC#"

# copy_object is capped at 5 GiB by S3; larger objects transition
# through a multipart copy (part copies of the same object onto
# itself). 256 MiB parts keep the part count ~20k for a 5 TiB object
# — well under S3's 10,000-part limit.
MAX_COPY_OBJECT_BYTES = 5 * 1024**3
MULTIPART_PART_SIZE = 256 * 1024**2
MULTIPART_MAX_PARTS = 10_000
MULTIPART_MAX_WORKERS = 4

# Listing routes carry optional client-side pagination; a token is
# the opaquely encoded LastEvaluatedKey.
DEFAULT_PAGE_LIMIT = 500


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


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def actor_from_event(event):
    """Identity of the signed-in caller as recorded by the JWT
    authorizer — used to attribute created/approved/executed actions
    so the decision trail is audit-ready. Claims are absent when the
    function runs outside the API (tests), which is fine."""
    claims = (
        event.get("requestContext", {})
        .get("authorizer", {})
        .get("jwt", {})
        .get("claims", {})
    )

    email = claims.get("email")
    subject = claims.get("sub")

    return email if email else subject


def is_approver(event):
    """True when the caller's JWT carries the Approvers group.

    HTTP API's JWT authorizer surfaces `cognito:groups` in
    requestContext.authorizer.jwt.claims. For a single group Cognito
    sends a plain string; multiple groups arrive comma-joined (a raw
    list is handled for robustness across API GW versions). Claims
    are absent outside the API (direct invokes/tests), where the
    check passes — authorization is the gateway's job there."""
    claims = (
        event.get("requestContext", {})
        .get("authorizer", {})
        .get("jwt", {})
        .get("claims", {})
    )

    raw = claims.get("cognito:groups")

    if not raw:
        return True

    if isinstance(raw, str):
        groups = [g.strip() for g in raw.split(",") if g.strip()]
    elif isinstance(raw, (list, tuple)):
        groups = [str(g).strip() for g in raw if str(g).strip()]
    else:
        groups = []

    return APPROVER_GROUP in groups


def conditional_failure(error):
    """True when a write was refused because its ConditionExpression
    did not hold — the atomic signal that another caller won the
    race. Covers the transactional and single-write forms."""
    if isinstance(error, ClientError):
        return error.response.get("Error", {}).get(
            "Code"
        ) in (
            "ConditionalCheckFailedException",
            "TransactionCanceledException",
        )
    return False


def race_lost(error):
    """True when the specific lost race is a failed condition check
    (including inside a cancellation reasons list)."""

    if not conditional_failure(error):
        return False

    if isinstance(error, ClientError):
        reasons = error.response.get(
            "CancellationReasons"
        ) or error.response.get("Error", {}).get("CancellationReasons")

        if isinstance(reasons, list):
            return any(
                reason.get("Code") == "ConditionalCheckFailed"
                or reason.get("Code") == "ConditionalCheckFailedException"
                for reason in reasons
                if isinstance(reason, dict)
            )

    return True


def engine_stale(document, aggregates):
    """True when the aggregate's view of the storage class no longer
    matches the documents table — a verified execution moved the
    object but this aggregate predates the move, so recomputing the
    forecast from it would propose the same migration twice and
    double-count its savings in the ledger."""

    aggregate_class = aggregates.get("current_storage_class")
    document_class = document.get("current_storage_class")

    if document_class is None:
        return False

    return str(aggregate_class) != str(document_class)


def to_dynamo(value):
    """boto3/DynamoDB write format: floats must be Decimal, applied
    recursively so nested engine figures survive a put_item."""

    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {key: to_dynamo(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [to_dynamo(item) for item in value]
    return value


def lambda_handler(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method")
    path = event.get("rawPath", "")

    if method == "POST" and path == "/decisions":
        return create_decision(event)

    if method == "POST" and path == "/decisions/propose-batch":
        return propose_batch(event)

    if method == "GET" and path == "/fleet/projection":
        return fleet_projection(event)

    if method == "GET" and path == "/decisions":
        return list_decisions(event)

    if method == "GET" and path == "/ledger":
        return realized_savings_ledger(event)
    if method == "POST" and path == "/ledger":
        return response(404, {"error": "Not found"})

    if method == "GET" and path.startswith("/decisions/") and path.endswith(
        "/execute"
    ):
        # GET /execute must 405: an HTML prefetch (or a curious
        # link crawl) would fire a real S3 transition. Execution is
        # POST-only.
        return response(405, {"error": "POST /decisions/{id}/execute"})

    if method == "POST" and path.startswith("/decisions/") and path.endswith(
        "/execute"
    ):
        return execute_decision(path.split("/")[-2], event)

    if method == "POST" and path.startswith("/decisions/") and path.endswith(
        "/approve"
    ):
        return update_state(path.split("/")[-2], STATE_APPROVED, event)

    if method == "POST" and path.startswith("/decisions/") and path.endswith(
        "/reject"
    ):
        return update_state(path.split("/")[-2], STATE_REJECTED, event)

    if method == "GET" and path.startswith("/decisions/"):
        return get_decision(path.split("/")[-1])

    return response(404, {"error": "Not found"})


def engine_result_for(document_id):
    """Aggregates-table item -> engine -> OptimizationResult, plus
    the documents-table item for the object key. Returns an error
    tuple when the pipeline has not aggregated this document."""

    aggregates = aggregates_table.get_item(
        Key={"document_id": document_id}
    ).get("Item")

    if not aggregates:
        return None, "document has no aggregate yet - run aggregation first"

    document = documents_table.get_item(
        Key={"document_id": document_id}
    ).get("Item")

    if not document:
        return None, "document not found"

    engine_input = from_aggregate_item(aggregates)

    return {
        "engine_input": engine_input,
        "result": optimize_document(engine_input, "B"),
        "aggregates": aggregates,
        "document": document,
    }, None


def create_decision(event):
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return response(400, {"error": "Request body must be valid JSON"})

    document_id = body.get("document_id")

    if not isinstance(document_id, str) or not document_id.strip():
        return response(400, {"error": "document_id must be a non-empty string"})

    # One pending decision per document: a duplicate proposal while
    # an earlier one is still proposed/approved would create a
    # double-execution risk.
    for state in (STATE_PROPOSED, STATE_APPROVED):
        pending = decisions_table.query(
            IndexName="document-index",
            KeyConditionExpression="document_id = :document_id",
            FilterExpression="decision_state = :state",
            ExpressionAttributeValues={
                ":document_id": document_id,
                ":state": state,
            },
        ).get("Items", [])

        if pending:
            return response(
                409,
                {
                    "error": f"decision for this document is already {state}",
                    "decision_id": pending[0]["decision_id"],
                },
            )

    engine, error = engine_result_for(document_id)

    if error:
        return response(409, {"error": error})

    result = engine["result"]

    # Stale-aggregate guard: if a verified execution already moved the
    # object and the aggregate predates it, re-running the engine would
    # propose the same migration again and double-count its savings.
    if engine_stale(engine["document"], engine["aggregates"]):
        return response(
            409,
            {
                "error": (
                    "aggregate is stale: the document's storage class "
                    "changed after this aggregate was computed - run "
                    "aggregation first"
                ),
            },
        )

    if result.recommended_storage_class == result.current_storage_class:
        return response(
            409,
            {
                "error": (
                    "the optimizer recommends keeping this document in "
                    f"{result.current_storage_class}: there is no "
                    "migration to approve"
                )
            },
        )

    decision_id = str(uuid.uuid4())

    item = record_proposal(decision_id, engine, result, actor_from_event(event))

    try:
        proposed = write_proposal(item)
    except Exception:
        logger.exception("failed to put decision %s", decision_id)
        return response(500, {"error": "Failed to record the decision"})

    if proposed:
        return response(201, item)
    return response(409, {"error": "decision for this document is already proposed"})


def open_decision_marker(item):
    """The per-document uniqueness record for the proposal
    transaction. `decision_state` partitions all markers under one
    GSI value so the batch proposer can find them with a cheap
    query instead of a full scan."""
    return {
        "decision_id": f"{OPEN_DECISION_PREFIX}{item['document_id']}",
        "open_decision_id": item["decision_id"],
        "document_id": item["document_id"],
        "decision_state": "__open_marker__",
    }


def write_proposal(item):
    """Race-proof proposal write: the decision AND the document's
    open-decision marker in one transaction, with the marker's
    attribute_not_exists condition as the only thing two concurrent
    proposals cannot both win. Returns True on success, False when
    the race was lost."""

    marker = open_decision_marker(item)

    try:
        dynamodb.meta.client.transact_write_items(
            TransactItems=[
                {
                    "Put": {
                        "TableName": DECISIONS_TABLE,
                        "Item": to_dynamo(marker),
                        "ConditionExpression":
                            "attribute_not_exists(decision_id)",
                    }
                },
                {
                    "Put": {
                        "TableName": DECISIONS_TABLE,
                        "Item": to_dynamo(item),
                    }
                },
            ]
        )
        return True

    except Exception as error:
        if race_lost(error):
            logger.info(
                "proposal for %s lost the uniqueness race",
                item["document_id"],
            )
            return False
        logger.exception("failed to put decision %s", item["decision_id"])
        raise


def record_proposal(decision_id, engine, result, actor):
    """Shared proposal record builder, identical data for the single
    and the batch propose paths (same fields, one place)."""

    # Sampling uncertainty: the observed 30-day count is one Poisson
    # draw, so the predicted saving carries its exact interval —
    # recomputed from the SAME aggregate the point estimate
    # consumed, at the same policy. The band endpoints are floored
    # like the point estimate (Policy B cannot cost-increase).
    band_low, band_high = savings_band(engine["engine_input"], POLICY_B)
    count_low, count_high = poisson_interval(
        engine["engine_input"].access_count
    )

    return {
        "decision_id": decision_id,
        "document_id": result.document_id,
        "object_key": engine["document"]["object_key"],
        "file_name": engine["document"].get("file_name"),
        "file_size": int(engine["document"]["file_size"]),
        "from_class": result.current_storage_class,
        "to_class": result.recommended_storage_class,
        "predicted_from_cost": result.current_cost,
        "predicted_to_cost": result.recommended_cost,
        "predicted_savings": result.savings,
        "predicted_savings_low": band_low,
        "predicted_savings_high": band_high,
        "forecast_access_band": [
            int(math.floor(count_low)),
            int(math.ceil(count_high)),
        ],
        "savings_percentage": result.savings_percentage,
        "policy": result.policy,
        "policy_conflict": result.policy_conflict,
        # Feature provenance: the aggregate the forecast consumed,
        # so a later reviewer knows how fresh the inputs were.
        "features_as_of": engine["aggregates"]["aggregation_timestamp"],
        "engine_input": {
            "access_count": engine["engine_input"].access_count,
            "access_frequency": engine["engine_input"].access_frequency,
            "days_since_last_access":
                engine["engine_input"].days_since_last_access,
        },
        "decision_state": STATE_PROPOSED,
        "created_by": actor,
        "created_at": now_iso(),
    }


def propose_batch(event):
    """POST /decisions/propose-batch — run the engine over the live
    aggregates and record proposals for every document whose
    recommendation is a real migration, up to a per-call cap. This
    makes the approval queue self-service: run aggregation, propose,
    then walk the proposals through approve -> execute -> verify."""

    if not is_approver(event):
        return response(403, {"error": "Approver role required for this action"})

    try:
        body = json.loads(event.get("body") or "{}")

        max_proposals = int(body.get("max", DEFAULT_PROPOSE_BATCH))

        if not (1 <= max_proposals <= MAX_PROPOSE_BATCH):
            return response(
                400,
                {
                    "error": (
                        f"max must be between 1 and "
                        f"{MAX_PROPOSE_BATCH}"
                    )
                },
            )
    except (json.JSONDecodeError, TypeError, ValueError):
        return response(400, {"error": "Request body must be valid JSON"})

    actor = actor_from_event(event)

    try:
        aggregates = scan_every(aggregates_table)
        documents = {
            item["document_id"]: item
            for item in scan_every(documents_table)
        }
    except Exception:
        logger.exception("propose-batch could not load the tables")
        return response(500, {"error": "Failed to load pipeline state"})

    # Documents that already have an open decision loop: proposing
    # twice would create a double-execution risk the same way the
    # single-document path guards against. The third partition is
    # the open-decision markers themselves, so a race-won proposal
    # is also seen here without a full-table scan.
    pending_documents = set()

    try:
        for state in (STATE_PROPOSED, STATE_APPROVED, "__open_marker__"):
            request = None

            while True:
                kwargs = dict(request or {})
                kwargs.setdefault("IndexName", "state-index")
                kwargs.setdefault(
                    "KeyConditionExpression", "#s = :state"
                )
                kwargs.setdefault(
                    "ExpressionAttributeNames", {"#s": "decision_state"}
                )
                kwargs.setdefault(
                    "ExpressionAttributeValues", {":state": state}
                )

                result = decisions_table.query(**kwargs)

                pending_documents.update(
                    item["document_id"]
                    for item in result.get("Items", [])
                )

                request = {"ExclusiveStartKey": last} if (
                    last := result.get("LastEvaluatedKey")
                ) else None

                if not request:
                    break

    except Exception:
        logger.exception("propose-batch could not scan pending decisions")
        return response(
            500, {"error": "Failed to check pending decisions"}
        )

    created = []
    skipped = []

    for aggregate in sorted(
        aggregates,
        key=lambda item: str(item.get("aggregation_timestamp") or ""),
        reverse=True,
    ):
        if len(created) >= max_proposals:
            break

        document_id = aggregate["document_id"]

        if document_id in pending_documents:
            skipped.append(
                {
                    "document_id": document_id,
                    "reason": "decision already pending",
                }
            )
            continue

        document = documents.get(document_id)

        if not document:
            skipped.append(
                {
                    "document_id": document_id,
                    "reason": "document not found",
                }
            )
            continue

        # A half-uploaded object is not the storage item the
        # metadata describes; sizing or migrating it would burn a
        # request and record fiction.
        if document.get("upload_status") == "UPLOAD_PENDING":
            skipped.append(
                {
                    "document_id": document_id,
                    "reason": "upload still pending (never confirmed)",
                }
            )
            continue

        if engine_stale(document, aggregate):
            stale = (
                document.get("current_storage_class") or "current tier"
            )
            skipped.append(
                {
                    "document_id": document_id,
                    "reason": (
                        f"stale aggregate: document moved to "
                        f"{stale} after the aggregate was computed"
                    ),
                }
            )
            continue

        result = optimize_document(
            from_aggregate_item(aggregate), POLICY_B
        )

        if (
            result.recommended_storage_class
            == result.current_storage_class
        ):
            # Not an error: the engine's verdict was "keep in
            # place", so there is nothing to approve.
            continue

        # An archived object (flexible/deep retrieval) cannot be
        # re-copied until a restore completes; queueing such a
        # decision would only fail at execution. Skip it honestly
        # and say why.
        if result.current_storage_class in RESTORE_REQUIRED_CLASSES:
            skipped.append(
                {
                    "document_id": document_id,
                    "reason": (
                        f"object is archived in "
                        f"{result.current_storage_class} - a restore "
                        "must complete before it can be re-tiered"
                    ),
                }
            )
            continue

        decision_id = str(uuid.uuid4())

        item = record_proposal(
            decision_id,
            {
                "document": document,
                "aggregates": aggregate,
                "engine_input": from_aggregate_item(aggregate),
            },
            result,
            actor,
        )

        try:
            proposed = write_proposal(item)
        except Exception:
            logger.exception(
                "propose-batch failed to put decision for %s",
                document_id,
            )
            skipped.append(
                {
                    "document_id": document_id,
                    "reason": "failed to record the decision",
                }
            )
            continue

        if not proposed:
            # A concurrent caller proposed the same document between
            # the pending-set snapshot and this write; the marker
            # transaction refused it.
            skipped.append(
                {
                    "document_id": document_id,
                    "reason": "decision already proposed (race)",
                }
            )
            continue

        created.append(item)
        pending_documents.add(document_id)

    return response(200, {
        "proposed_count": len(created),
        "proposed": created,
        "skipped": skipped[:MAX_PROPOSE_BATCH],
        "aggregates_scanned": len(aggregates),
        "basis": (
            "Proposals recorded by re-running the engine over the "
            "live aggregates - same model as the offline experiments"
        ),
    })


def list_decisions(event):
    query = event.get("queryStringParameters") or {}
    state = query.get("state")

    try:
        limit, start_key = page_request(query)
    except ValueError as error:
        return response(400, {"error": str(error)})

    try:
        # Page through the full result set: a bare query/scan returns
        # only the first ~1 MB page and would silently truncate the
        # queue and the history tab. With a client `limit` only ONE
        # page is served and the continuation key is encoded in the
        # response's next_token.
        items = []
        request = {} if start_key else None
        last_evaluated_key = None

        while True:
            kwargs = dict(request or {})
            if start_key:
                kwargs.setdefault("ExclusiveStartKey", start_key)
                start_key = None

            if limit:
                kwargs.setdefault("Limit", limit)

            if state:
                kwargs.setdefault("IndexName", "state-index")
                kwargs.setdefault(
                    "KeyConditionExpression",
                    "#s = :state",
                )
                kwargs.setdefault(
                    "ExpressionAttributeNames",
                    {"#s": "decision_state"},
                )
                kwargs.setdefault(
                    "ExpressionAttributeValues",
                    {":state": state},
                )

            # The scan/query reads the raw table, which includes the
            # one-open-decision-per-document marker items; they are
            # bookkeeping, not decisions, and would poison the frontend
            # queues (their states are "unknown", so the History tab
            # buckets them and its rows crash on the missing fields).
            values = kwargs.get("ExpressionAttributeValues") or {}
            values[":marker_prefix"] = OPEN_DECISION_PREFIX
            kwargs["ExpressionAttributeValues"] = values
            kwargs.setdefault(
                "FilterExpression",
                "NOT begins_with(decision_id, :marker_prefix)",
            )

            if state:
                result = decisions_table.query(**kwargs)
            else:
                result = decisions_table.scan(**kwargs)

            items.extend(result.get("Items", []))

            last_evaluated_key = result.get("LastEvaluatedKey")

            request = {"ExclusiveStartKey": last} if (
                last := last_evaluated_key
            ) else None

            if not request:
                break

            if limit and len(items) >= limit:
                break

        body = page_result(items, "decisions", last_evaluated_key if (
            limit and items
        ) else None)

        return response(200, body)

    except Exception:
        logger.exception("failed to list decisions")
        return response(500, {"error": "Failed to list decisions"})


def get_decision(decision_id):
    if not decision_id:
        return response(400, {"error": "decision_id is required"})

    try:
        item = decisions_table.get_item(
            Key={"decision_id": decision_id}
        ).get("Item")

        if not item:
            return response(404, {"error": "Decision not found"})

        return response(200, item)

    except Exception:
        logger.exception("failed to retrieve decision %s", decision_id)
        return response(500, {"error": "Failed to retrieve decision"})


def page_request(query):
    """Decode optional client pagination params for a listing
    route. Returns (limit, start_key); `limit` is None when unset
    (the caller then pages everything server-side — the historical
    behavior clients still rely on). Raises ValueError on a
    malformed request."""
    limit_raw = (query or {}).get("limit")

    if limit_raw in (None, ""):
        limit = None
    else:
        limit = int(limit_raw)
        if not (1 <= limit <= 1000):
            raise ValueError("limit must be between 1 and 1000")

    token = (query or {}).get("next_token")

    if token in (None, ""):
        start_key = None
    else:
        start_key = json.loads(
            base64.urlsafe_b64decode(token.encode()).decode()
        )

        if not isinstance(start_key, dict):
            raise ValueError("next_token is malformed")

    return limit, start_key


def page_result(items, key_name, last_evaluated_key):
    """Body for a page-aware listing: `next_token` only when a page
    was actually capped, so callers page on until it disappears.
    The item key name stays each route's historical wire shape."""
    body = {}

    if items is not None:
        body[key_name] = items

    if last_evaluated_key is not None:
        body["next_token"] = base64.urlsafe_b64encode(
            json.dumps(last_evaluated_key).encode()
        ).decode()

    return body


def update_state(decision_id, new_state, event=None):
    """proposed -> approved / rejected. The approved/rejected stamps
    are reserved words on the wire, so their attribute names go
    through ExpressionAttributeNames. The state flip is atomic
    (ConditionExpression): two concurrent callers cannot both win —
    the loser gets the same 409 wording it would see serially."""

    if not decision_id:
        return response(400, {"error": "decision_id is required"})

    if not is_approver(event):
        return response(403, {"error": "Approver role required for this action"})

    try:
        item = decisions_table.get_item(
            Key={"decision_id": decision_id}
        ).get("Item")

        if not item:
            return response(404, {"error": "Decision not found"})

        if item["decision_state"] != STATE_PROPOSED:
            return response(
                409,
                {
                    "error": (
                        f"decision is {item['decision_state']}, "
                        f"only a proposed decision can become {new_state}"
                    )
                },
            )

        try:
            return _transition_proposed(
                decision_id, item, new_state, event
            )
        except Exception as error:
            if conditional_failure(error):
                # Another caller flipped the state between the
                # get_item above and this write.
                return response(
                    409,
                    {
                        "error": (
                            "decision is no longer proposed - changed "
                            "by a concurrent action"
                        )
                    },
                )
            raise

    except Exception:
        logger.exception(
            "failed to move decision %s to %s", decision_id, new_state
        )
        return response(
            500,
            {"error": f"Failed to move decision to {new_state}"},
        )


def _transition_proposed(decision_id, item, new_state, event):
    """The actual proposed -> approved|rejected write, split out so
    the conditional-failure race maps cleanly to a 409."""

    stamp = "approved_at" if new_state == STATE_APPROVED else "rejected_at"

    decisions_table.update_item(
        Key={"decision_id": decision_id},
        UpdateExpression=(
            "SET decision_state = :state, #stamp_attr = :at, "
            "#actor_attr = :actor"
        ),
        ConditionExpression="decision_state = :expected",
        ExpressionAttributeNames={
            "#stamp_attr": stamp,
            "#actor_attr": f"{new_state}_by",
        },
        ExpressionAttributeValues={
            ":state": new_state,
            ":at": now_iso(),
            ":actor": actor_from_event(event or {}),
            ":expected": STATE_PROPOSED,
        },
    )

    if new_state == STATE_REJECTED:
        # The document's open-decision slot frees up; a fresh
        # proposal may be created for it again.
        _release_open_decision(item["document_id"])

    return response(200, {"decision_id": decision_id, "decision_state": new_state})


def _release_open_decision(document_id):
    """Best-effort deletion of the open-decision marker. Best-effort
    by design: a leaked marker only makes the next proposal for this
    document hit the 409 wording and can be cleared by hand; a HARD
    failure here would strand a verified decision's success as an
    HTTP error."""
    try:
        decisions_table.delete_item(
            Key={
                "decision_id": f"{OPEN_DECISION_PREFIX}{document_id}"
            }
        )
    except Exception:
        logger.exception(
            "could not release open-decision marker for %s", document_id
        )


def execute_decision(decision_id, event=None):
    """The only transition that touches S3: copy the object into the
    approved storage class, then verify with a HEAD request before
    anything is claimed as savings. A verified copy is what turns a
    predicted saving into a realized one — and moving the documents
    table's own current_storage_class is what stops the same
    migration being proposed, approved and counted a second time."""

    if not decision_id:
        return response(400, {"error": "decision_id is required"})

    if not is_approver(event):
        return response(403, {"error": "Approver role required for this action"})

    actor = actor_from_event(event or {})

    try:
        item = decisions_table.get_item(
            Key={"decision_id": decision_id}
        ).get("Item")

        if not item:
            return response(404, {"error": "Decision not found"})

        if item["decision_state"] != STATE_APPROVED:
            return response(
                409,
                {
                    "error": (
                        f"decision is {item['decision_state']}, "
                        "only an approved decision can be executed"
                    )
                },
            )
    except Exception:
        logger.exception("failed to load decision %s", decision_id)
        return response(500, {"error": "Failed to load the decision"})

    to_class = item["to_class"]
    object_key = item["object_key"]
    predicted_savings = float(item["predicted_savings"])
    predicted_low = float(item.get("predicted_savings_low") or predicted_savings)
    predicted_high = float(
        item.get("predicted_savings_high") or predicted_savings
    )

    try:
        # Atomic claim: put the approved decision into this request's
        # hands before any S3 call. A concurrent second executor
        # fails the condition and 409s — double execution becomes
        # impossible because exactly one caller wins the condition
        # while the decision still reads approved.
        executed_at = now_iso()

        decisions_table.update_item(
            Key={"decision_id": decision_id},
            UpdateExpression=(
                "SET execute_claim = :claim, executed_by = :actor"
            ),
            ConditionExpression="decision_state = :expected",
            ExpressionAttributeValues={
                ":claim": executed_at,
                ":actor": actor,
                ":expected": STATE_APPROVED,
            },
        )
    except Exception as error:
        if conditional_failure(error):
            return response(
                409,
                {
                    "error": (
                        "decision is no longer approved - claimed by "
                        "a concurrent execution"
                    )
                },
            )
        logger.exception("failed to claim decision %s", decision_id)
        return response(500, {"error": "Failed to claim the decision"})

    try:
        verified_class, verified_size, transition_error = transition_object(
            object_key, to_class
        )

        if transition_error:
            raise RuntimeError(transition_error)

        if verified_class not in ACCEPTED_HEAD_CLASSES.get(
            to_class, {to_class}
        ):
            state = STATE_FAILED
            verification_error = (
                f"object reports storage class {verified_class!r} "
                f"after transition to {to_class}"
            )
        else:
            verified_at = now_iso()

            decisions_table.update_item(
                Key={"decision_id": decision_id},
                UpdateExpression=(
                    "SET decision_state = :state, "
                    "executed_at = :executed_at, "
                    "#verified_at = :verified_at, "
                    "verified_storage_class = :verified_class, "
                    "verified_size = :verified_size, "
                    "realized_savings = :savings, "
                    "realized_savings_low = :savings_low, "
                    "realized_savings_high = :savings_high, "
                    "executed_by = :actor "
                    "REMOVE execute_claim"
                ),
                ExpressionAttributeNames={"#verified_at": "verified_at"},
                ExpressionAttributeValues={
                    ":state": STATE_VERIFIED,
                    ":executed_at": executed_at,
                    ":verified_at": verified_at,
                    ":verified_class": verified_class,
                    ":verified_size": verified_size,  # int
                    ":savings": Decimal(str(predicted_savings)),
                    ":savings_low": Decimal(str(predicted_low)),
                    ":savings_high": Decimal(str(predicted_high)),
                    ":actor": actor,
                },
            )

            # Keep the documents table in step with S3: the next
            # aggregation reads this class, so the engine re-runs
            # against the post-transition state and cannot propose
            # the same migration twice. Without this the ledger
            # double-counts the savings on every re-proposal.
            documents_table.update_item(
                Key={"document_id": item["document_id"]},
                UpdateExpression=(
                    "SET current_storage_class = :class"
                ),
                ExpressionAttributeValues={
                    ":class": verified_class,
                },
            )

            # The document's open-decision slot opens: a NEW decision
            # (against post-transition state) may be proposed again.
            _release_open_decision(item["document_id"])

            return response(
                200,
                {
                    "decision_id": decision_id,
                    "decision_state": STATE_VERIFIED,
                    "verified_storage_class": verified_class,
                    "verified_size": verified_size,
                    # Realized savings: the forecast carried by the
                    # decision whose copy S3 HEAD-verified. This is
                    # the predicted annual figure behind a verified
                    # transition - never an invoice number.
                    "realized_savings": predicted_savings,
                    "realized_savings_low": predicted_low,
                    "realized_savings_high": predicted_high,
                },
            )

    except Exception as error:
        logger.exception(
            "execute of decision %s failed during the S3 transition",
            decision_id,
        )
        state = STATE_FAILED
        verification_error = str(error)

    decisions_table.update_item(
        Key={"decision_id": decision_id},
        UpdateExpression=(
            "SET decision_state = :state, "
            "verification_error = :error, "
            "executed_by = :actor, executed_at = :executed_at "
            "REMOVE execute_claim"
        ),
        ExpressionAttributeValues={
            ":state": state,
            ":error": verification_error,
            ":actor": actor,
            ":executed_at": executed_at,
        },
    )

    _release_open_decision(item["document_id"])

    # A failed verification is a determined decision outcome (the
    # command completed; the loop ended in `failed`), not a server
    # fault — 200 keeps the API 5XX alarm meaningful and lets the
    # frontend read the failure payload instead of a generic 500.
    return response(
        200,
        {
            "decision_id": decision_id,
            "decision_state": state,
            "verification_error": verification_error,
        },
    )


def transition_object(object_key, to_class):
    """Move the object into `to_class` (self-copy) and return
    (verified_class, verified_size, error). `to_class` and the
    returned class are engine billing names — the S3 API enum is
    applied only at the very boundary of the copy. Objects over the
    5 GiB copy_object ceiling transition through a multipart copy
    instead. Any S3 failure comes back as `error` so the caller's
    failure path records it rather than a half-claimed saving."""

    try:
        # The pre-transition HEAD doubles as the part planner and
        # 404s early on an already-deleted object.
        source_head = s3.head_object(
            Bucket=DOCUMENTS_BUCKET, Key=object_key
        )
        size = int(source_head.get("ContentLength") or 0)

        # Billing name -> API enum, e.g. GLACIER_INSTANT_RETRIEVAL
        # -> GLACIER_IR; the standard-tier names are identical in
        # both vocabularies and pass through unchanged.
        api_class = S3_API_CLASS_BY_ENGINE_NAME.get(to_class, to_class)

        source_class = source_head.get("StorageClass") or "STANDARD"

        # An archived object cannot be re-tiered by CopyObject: the
        # restore must run first (hours for flexible retrieval,
        # up to ~48h for deep archive). Record a clear failure
        # instead of a cryptic AccessDenied/InvalidObjectState.
        if (
            source_class != api_class
            and source_class in RESTORE_REQUIRED_API_CLASSES
        ):
            return None, None, (
                f"object is archived in {ENGINE_CLASS_BY_S3_API_NAME.get(source_class, source_class)} "
                "and cannot be re-tiered until a restore completes"
            )

        if size > MAX_COPY_OBJECT_BYTES:
            _multipart_transition(object_key, api_class, size)
        else:
            # Changing storage class = copying the object onto
            # itself with a target storage class; the copy is a
            # transition request billed per the pricing snapshot
            # the forecast consumed.
            s3.copy_object(
                Bucket=DOCUMENTS_BUCKET,
                Key=object_key,
                CopySource={
                    "Bucket": DOCUMENTS_BUCKET,
                    "Key": object_key,
                },
                StorageClass=api_class,
            )
    except Exception as error:
        return None, None, str(error)

    head = s3.head_object(Bucket=DOCUMENTS_BUCKET, Key=object_key)

    reported_class = head.get("StorageClass") or "STANDARD"

    # API enum -> billing name, e.g. DEEP_ARCHIVE ->
    # GLACIER_DEEP_ARCHIVE, so the verification comparison against
    # `to_class` and the documents-table write-back speak the
    # engine's vocabulary.
    verified_class = ENGINE_CLASS_BY_S3_API_NAME.get(
        reported_class, reported_class
    )

    return (
        verified_class,
        head.get("ContentLength"),
        None,
    )


def _multipart_transition(object_key, to_class, size):
    """Multipart self-copy for >5 GiB objects: bounded part copies
    with CopySourceRange into a multipart upload created with the
    target storage class (already the S3 API enum), then complete.
    Any failure aborts the upload so no orphaned parts are left to
    bill."""
    part_size = max(
        MULTIPART_PART_SIZE,
        math.ceil(size / MULTIPART_MAX_PARTS),
    )
    part_count = math.ceil(size / part_size)

    created = s3.create_multipart_upload(
        Bucket=DOCUMENTS_BUCKET,
        Key=object_key,
        StorageClass=to_class,
    )
    upload_id = created["UploadId"]

    # S3 part numbers are 1-based; CopySourceRange is inclusive and
    # must not extend past the last byte.
    ranges = [
        (
            index + 1,
            index * part_size,
            min(size, (index + 1) * part_size) - 1,
        )
        for index in range(part_count)
    ]

    try:
        with ThreadPoolExecutor(
            max_workers=MULTIPART_MAX_WORKERS
        ) as pool:
            futures = {
                pool.submit(
                    s3.upload_part_copy,
                    Bucket=DOCUMENTS_BUCKET,
                    Key=object_key,
                    UploadId=upload_id,
                    PartNumber=part_number,
                    CopySource={
                        "Bucket": DOCUMENTS_BUCKET,
                        "Key": object_key,
                    },
                    CopySourceRange=f"bytes={start}-{end}",
                ): part_number
                for part_number, start, end in ranges
            }

            parts = {
                futures[future]: future.result()["CopyPartResult"]["ETag"]
                for future in futures
            }

        s3.complete_multipart_upload(
            Bucket=DOCUMENTS_BUCKET,
            Key=object_key,
            UploadId=upload_id,
            MultipartUpload={
                "Parts": [
                    {
                        "PartNumber": part_number,
                        "ETag": parts[part_number],
                    }
                    for part_number in sorted(parts)
                ]
            },
        )
    except Exception:
        logger.exception(
            "multipart transition of %s failed - aborting upload %s",
            object_key,
            upload_id,
        )
        s3.abort_multipart_upload(
            Bucket=DOCUMENTS_BUCKET,
            Key=object_key,
            UploadId=upload_id,
        )
        raise


def scan_every(table):
    """Full scan across pages — a bare scan() returns only the first
    ~1 MB page and would silently truncate the fleet."""

    items = []
    scan_kwargs = {}

    while True:
        result = table.scan(**scan_kwargs)

        items.extend(result.get("Items", []))

        last_key = result.get("LastEvaluatedKey")

        if not last_key:
            break

        scan_kwargs["ExclusiveStartKey"] = last_key

    return items


def fleet_projection(event):
    """GET /fleet/projection — run the engine over EVERY aggregate in
    the live aggregates table and project the fleet's modeled
    12-month cost under the chosen policy.

    This is the live counterpart of the offline "projected fleet
    annual cost" experiment panel: same engine, same pricing
    snapshot, but computed from live aggregate state on request.
    Response carries its own `basis` string so no client can mistake
    the model for an invoice."""

    query = event.get("queryStringParameters") or {}
    policy = (query.get("policy") or "B").upper()

    if policy not in (POLICY_A, POLICY_B):
        return response(400, {"error": "policy must be A or B"})

    try:
        aggregates = scan_every(aggregates_table)

        if not aggregates:
            return response(
                409,
                {
                    "error": (
                        "no aggregates yet - run aggregation first"
                    ),
                },
            )

        # Document classes guard against stale aggregates exactly
        # like create_decision does.
        documents = {
            item["document_id"]: item
            for item in scan_every(documents_table)
        }

        baseline_total = 0.0
        optimized_total = 0.0
        savings_total = 0.0
        savings_band_total = [0.0, 0.0]
        migrations = 0
        stale_aggregates = 0
        pending_uploads = 0
        migrations_by_target = {}

        # Tier allocation for the panel's stacked bars: document
        # counts and billable GB per class, current vs recommended.
        priced_tiers = load_pricing()["storage_classes"]

        def billable_gb(storage_class, file_size_bytes):
            # A class missing from the pricing snapshot has no
            # minimum billable sizing; fall back to raw GB.
            tier = priced_tiers.get(storage_class)

            if not tier:
                return int(file_size_bytes) / 1e9

            return calculate_billable_size_gb(
                tier, int(file_size_bytes)
            )

        current_tiers = {}
        recommended_tiers = {}

        def add_allocation(allocation, storage_class, file_size_bytes):
            bucket = allocation.setdefault(
                storage_class,
                {"document_count": 0, "billable_gb": 0.0},
            )
            bucket["document_count"] += 1
            bucket["billable_gb"] += billable_gb(
                storage_class, file_size_bytes
            )

        for aggregate in aggregates:
            engine_input = from_aggregate_item(aggregate)

            document = documents.get(engine_input.document_id)

            if document and engine_stale(document, aggregate):
                stale_aggregates += 1
                continue

            if document and (
                document.get("upload_status") == "UPLOAD_PENDING"
            ):
                pending_uploads += 1
                continue

            result = optimize_document(engine_input, policy)

            baseline_total += result.current_cost
            optimized_total += result.recommended_cost
            savings_total += result.savings

            if result.savings > 0:
                band_low, band_high = savings_band(
                    engine_input, policy
                )
                savings_band_total[0] += band_low
                savings_band_total[1] += band_high

            add_allocation(
                current_tiers,
                result.current_storage_class,
                engine_input.file_size_bytes,
            )
            add_allocation(
                recommended_tiers,
                result.recommended_storage_class,
                engine_input.file_size_bytes,
            )

            if (
                result.recommended_storage_class
                != result.current_storage_class
            ):
                migrations += 1

                target = migrations_by_target.setdefault(
                    result.recommended_storage_class,
                    {"count": 0, "savings_annual": 0.0},
                )
                target["count"] += 1
                target["savings_annual"] += result.savings

        savings_percentage = (
            (savings_total / baseline_total) * 100
            if baseline_total > 0
            else 0.0
        )

        def allocation_rows(allocation):
            return [
                {
                    "storage_class": storage_class,
                    "document_count": bucket["document_count"],
                    "billable_gb": round(bucket["billable_gb"], 4),
                }
                for storage_class, bucket in sorted(allocation.items())
            ]

        projected = round(savings_total, 4)

        return response(
            200,
            {
                "policy": policy,
                "documents": len(documents),
                "aggregated_documents": len(aggregates),
                "stale_aggregates": stale_aggregates,
                "pending_uploads": pending_uploads,
                "baseline_projected_annual": round(baseline_total, 4),
                "optimized_projected_annual": round(optimized_total, 4),
                "projected_savings": projected,
                # Sum of each document's predicted savings band — a
                # fleet envelope, NOT a confidence interval on the
                # fleet total (bands don't narrow when summed).
                "projected_savings_band": [
                    round(savings_band_total[0], 4),
                    round(savings_band_total[1], 4),
                ],
                "savings_percentage": round(savings_percentage, 2),
                "migrations": migrations,
                "migrations_by_target": migrations_by_target,
                "current_tier_allocation": allocation_rows(current_tiers),
                "recommended_tier_allocation": allocation_rows(
                    recommended_tiers
                ),
                "basis": (
                    "Modeled 12-month projection from running the "
                    "engine over every aggregate in the live "
                    "aggregates table - not invoice data"
                ),
            },
        )

    except Exception:
        logger.exception("fleet projection failed")
        return response(500, {"error": "Failed to project fleet costs"})


def realized_savings_ledger(event=None):
    """Every verified transition with its savings - the proof
    ledger. Each entry's numbers are anchored by an S3 HEAD
    verification, not by an invoice."""

    query = (event or {}).get("queryStringParameters") or {}

    try:
        limit, start_key = page_request(query)
    except ValueError as error:
        return response(400, {"error": str(error)})

    try:
        items = []
        request = {} if start_key else None
        last_evaluated_key = None

        while True:
            kwargs = dict(request or {})
            if start_key:
                kwargs.setdefault("ExclusiveStartKey", start_key)
                start_key = None

            if limit:
                kwargs.setdefault("Limit", limit)

            kwargs.setdefault("IndexName", "state-index")
            kwargs.setdefault(
                "KeyConditionExpression",
                "#s = :state",
            )
            kwargs.setdefault(
                "ExpressionAttributeNames",
                {"#s": "decision_state"},
            )
            kwargs.setdefault(
                "ExpressionAttributeValues",
                {":state": STATE_VERIFIED},
            )

            result = decisions_table.query(**kwargs)

            items.extend(result.get("Items", []))

            last_evaluated_key = result.get("LastEvaluatedKey")

            request = {"ExclusiveStartKey": last} if (
                last := last_evaluated_key
            ) else None

            if not request:
                break

            if limit and len(items) >= limit:
                break

        rows = items

    except Exception:
        logger.exception("failed to build the ledger")
        return response(500, {"error": "Failed to build the ledger"})

    total_savings = sum(
        float(row["realized_savings"]) for row in rows
        if row.get("realized_savings") is not None
    )

    # Band totals: each entry carries the Poisson band of its own
    # forecast; the sum is the honest fleet envelope (sums of bands,
    # NOT a confidence interval on the fleet total). Entries without
    # a band (legacy decisions) contribute their point value to both
    # ends.
    bands_low = sum(
        float(row["realized_savings_low"])
        if row.get("realized_savings_low") is not None
        else (
            float(row["realized_savings"])
            if row.get("realized_savings") is not None
            else 0.0
        )
        for row in rows
    )

    bands_high = sum(
        float(row["realized_savings_high"])
        if row.get("realized_savings_high") is not None
        else (
            float(row["realized_savings"])
            if row.get("realized_savings") is not None
            else 0.0
        )
        for row in rows
    )

    body = page_result(None, "entries", last_evaluated_key if (
        limit and items
    ) else None)
    body["entries"] = rows
    body["total_realized_annual_savings"] = total_savings
    body["total_realized_savings_band"] = [
        round(bands_low, 2), round(bands_high, 2)
    ]
    body["basis"] = (
        "predicted annual savings carried by decisions whose S3 "
        "copy was verified via HEAD - not invoice data"
    )

    return response(200, body)