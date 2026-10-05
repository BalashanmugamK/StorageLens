import json
import os
import random
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from threading import local

import boto3
import botocore.config
from botocore.exceptions import BotoCoreError, ClientError
from boto3.dynamodb.conditions import Attr, Key


OBSERVATION_PERIOD_DAYS = 30
SECONDS_PER_DAY = 86400

# The access-history queries run with bounded concurrency in a
# fixed-size thread pool. The bound is deliberately modest: the
# AccessHistoryTable is billed on demand, so a handful of dozen
# workers removes the N+1 bottleneck without risking throttling.
MAX_CONCURRENT_QUERIES = 16

# The resource's underlying client carries one connection pool
# shared by every thread, so it must be sized to the worker
# bound — otherwise the pool becomes the hidden limiter. The
# region comes from the Lambda runtime's AWS_REGION environment
# variable (resolved by boto3), matching the documents Lambda.
dynamodb = boto3.resource(
    "dynamodb",
    config=botocore.config.Config(
        max_pool_connections=MAX_CONCURRENT_QUERIES
    ),
)

DOCUMENTS_TABLE_NAME = os.environ["DOCUMENTS_TABLE"]
ACCESS_HISTORY_TABLE_NAME = os.environ["ACCESS_HISTORY_TABLE"]
AGGREGATES_TABLE_NAME = os.environ["AGGREGATES_TABLE"]

documents_table = dynamodb.Table(DOCUMENTS_TABLE_NAME)
access_history_table = dynamodb.Table(ACCESS_HISTORY_TABLE_NAME)
aggregates_table = dynamodb.Table(AGGREGATES_TABLE_NAME)

# Transient DynamoDB errors are retried with bounded exponential
# backoff plus jitter before the aggregation is failed.
MAX_QUERY_ATTEMPTS = 5
RETRY_BACKOFF_SECONDS = 0.2
RETRY_BACKOFF_CAP_SECONDS = 2.0

RETRYABLE_ERROR_CODES = frozenset(
    [
        "ProvisionedThroughputExceeded",
        "ThrottlingException",
        "LimitExceededException",
        "InternalServerError",
        "RequestLimitExceeded",
        "ServiceUnavailable",
        "TransactionInProgressException",
        "TransactionConflictException",
    ]
)

# Resources are generally not thread-safe, so worker threads get
# their own Table handle in thread-local storage.
_thread_local = local()


def get_access_history_table():
    """Return the calling thread's AccessHistoryTable handle."""

    table = getattr(
        _thread_local,
        "access_history_table",
        None,
    )

    if table is None:
        table = dynamodb.Table(ACCESS_HISTORY_TABLE_NAME)

        _thread_local.access_history_table = table

    return table


def is_retryable_query_error(error):
    """
    Return True only for transient errors worth retrying:
    DynamoDB throttling/server-side hiccups and low-level
    connection failures in the SDK.
    """

    if isinstance(error, ClientError):
        error_code = error.response.get(
            "Error", {}
        ).get("Code", "")

        return error_code in RETRYABLE_ERROR_CODES

    # Connection resets, timeouts, credentials refresh
    # failures at the SDK boundary.
    return isinstance(error, BotoCoreError)


def query_access_history(query_kwargs):
    """
    Run one AccessHistoryTable query with bounded exponential
    backoff on transient errors.

    On a final failure the error propagates, exactly like the
    original sequential implementation.
    """

    attempt = 0

    while True:
        attempt += 1

        try:
            return get_access_history_table().query(
                **query_kwargs
            )

        except (ClientError, BotoCoreError) as error:
            if not is_retryable_query_error(error):
                raise

            if attempt >= MAX_QUERY_ATTEMPTS:
                raise

            backoff = min(
                RETRY_BACKOFF_CAP_SECONDS,
                RETRY_BACKOFF_SECONDS
                * (2 ** (attempt - 1)),
            )

            time.sleep(
                backoff * random.uniform(0.5, 1.5)
            )


def response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json"
        },
        "body": json.dumps(
            body,
            default=lambda value: int(value)
            if isinstance(value, Decimal)
            else str(value)
        )
    }


def parse_timestamp(timestamp):
    """Convert an ISO timestamp into a timezone-aware datetime."""

    parsed = datetime.fromisoformat(
        str(timestamp).replace("Z", "+00:00")
    )

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed


def compute_aggregate_fields(document, access_events, now):
    """
    Derive the 10-field aggregate for one document.

    Pure calculation with no AWS access so it can be unit-tested
    directly. Returns a DynamoDB-ready item.

    access_events must already be the events DynamoDB returned for
    this document; this helper still re-checks the access type and
    the 30-day window so the math is deterministic on its own.
    """

    cutoff = now - timedelta(days=OBSERVATION_PERIOD_DAYS)

    download_events = []

    for event in access_events:
        if event.get("access_type") != "DOWNLOAD":
            continue

        event_time = parse_timestamp(event["access_timestamp"])

        if event_time < cutoff:
            continue

        download_events.append(
            (event_time, event["access_timestamp"])
        )

    access_count = len(download_events)

    if download_events:
        last_access_time, last_accessed_timestamp = max(
            download_events,
            key=lambda pair: pair[0]
        )

        days_since_last_access = Decimal(
            str(
                (now - last_access_time).total_seconds()
                / SECONDS_PER_DAY
            )
        )
    else:
        last_accessed_timestamp = None
        days_since_last_access = None

    return {
        "document_id": document["document_id"],
        "file_size_bytes": int(document["file_size"]),
        "current_storage_class": document["current_storage_class"],
        "upload_timestamp": document["upload_timestamp"],
        "last_accessed_timestamp": last_accessed_timestamp,
        "access_count": access_count,
        "days_since_last_access": days_since_last_access,
        "access_frequency": Decimal(
            str(access_count / OBSERVATION_PERIOD_DAYS)
        ),
        "aggregation_timestamp": now.isoformat().replace(
            "+00:00", "Z"
        ),
        "document_state": document.get("document_state"),
    }


def fetch_all_documents():
    """Scan every document from DocumentsTable across pages."""

    documents = []
    scan_kwargs = {}

    while True:
        result = documents_table.scan(**scan_kwargs)

        documents.extend(result.get("Items", []))

        last_key = result.get("LastEvaluatedKey")

        if not last_key:
            break

        scan_kwargs["ExclusiveStartKey"] = last_key

    return documents


def fetch_recent_access_events(document_id, cutoff):
    """
    Query DOWNLOAD events for one document within the
    30-day observation window.

    The window filtering happens in the DynamoDB query
    condition, not in Python. Uses the calling thread's own
    table handle and bounded retries on transient errors.
    """

    query_kwargs = {
        "KeyConditionExpression": (
            Key("document_id").eq(document_id)
            & Key("access_timestamp").gte(cutoff.isoformat())
        ),
        "FilterExpression": Attr("access_type").eq("DOWNLOAD"),
    }

    events = []

    while True:
        result = query_access_history(query_kwargs)

        events.extend(result.get("Items", []))

        last_key = result.get("LastEvaluatedKey")

        if not last_key:
            break

        query_kwargs["ExclusiveStartKey"] = last_key

    return events


def build_aggregate_for_document(
    document,
    cutoff,
    now,
    fetch_events=None,
):
    """
    Full per-document aggregation step: fetch the document's
    recent DOWNLOAD events, then compute its aggregate fields.

    fetch_events is injectable so the sequential and parallel
    aggregation paths can be proven identical against the same
    synthetic event streams in tests. Production always uses
    fetch_recent_access_events.
    """

    if fetch_events is None:
        fetch_events = fetch_recent_access_events

    events = fetch_events(
        document["document_id"],
        cutoff,
    )

    return compute_aggregate_fields(
        document,
        events,
        now,
    )


def aggregate_documents_sequential(
    documents,
    cutoff,
    now,
    fetch_events=None,
):
    """
    Original access pattern: one sequential access-history query
    per document. Kept as the reference implementation that all
    results must match.

    fetch_events is injectable for the identity tests; production
    always uses fetch_recent_access_events.
    """

    return [
        build_aggregate_for_document(
            document,
            cutoff,
            now,
            fetch_events,
        )
        for document in documents
    ]


def aggregate_documents_parallel(
    documents,
    cutoff,
    now,
    fetch_events=None,
):
    """
    Bounded-parallel aggregation: dispatch the per-document
    access-history queries to a fixed-size thread pool so the
    N+1 round-trips overlap, while producing the exact same
    aggregate items in document order.

    fetch_events is injectable for the identity tests; production
    always uses fetch_recent_access_events.
    """

    if not documents:
        return []

    worker_count = min(
        MAX_CONCURRENT_QUERIES,
        len(documents),
    )

    with ThreadPoolExecutor(
        max_workers=worker_count
    ) as executor:
        aggregates = list(
            executor.map(
                lambda document: (
                    build_aggregate_for_document(
                        document,
                        cutoff,
                        now,
                        fetch_events,
                    )
                ),
                documents,
            )
        )

    return aggregates


def write_aggregates(aggregates):
    """Write aggregate items to DocumentAggregatesTable."""

    with aggregates_table.batch_writer() as batch:
        for aggregate in aggregates:
            batch.put_item(Item=aggregate)


def run_aggregation():
    try:
        # One reference timestamp for the whole run: cutoff,
        # days_since_last_access, and aggregation_timestamp.
        now = datetime.now(timezone.utc)

        cutoff = now - timedelta(days=OBSERVATION_PERIOD_DAYS)

        documents = fetch_all_documents()

        aggregates = aggregate_documents_parallel(
            documents,
            cutoff,
            now,
        )

        write_aggregates(aggregates)

        return response(
            200,
            {
                "message": "Aggregation completed",
                "documents_processed": len(aggregates)
            }
        )

    except Exception:
        return response(
            500,
            {
                "error": "Aggregation failed"
            }
        )


def lambda_handler(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method")
    path = event.get("rawPath", "")

    if method == "POST" and path == "/aggregates/run":
        return run_aggregation()

    return response(
        404,
        {
            "error": "Not found"
        }
    )