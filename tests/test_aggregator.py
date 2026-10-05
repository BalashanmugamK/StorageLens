import os
import sys
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Set env vars before importing the Lambda module, which reads
# them at import time. No AWS credentials are required.
os.environ.setdefault("DOCUMENTS_TABLE", "test-documents")
os.environ.setdefault("ACCESS_HISTORY_TABLE", "test-access-history")
os.environ.setdefault("AGGREGATES_TABLE", "test-aggregates")

from backend.lambdas.aggregates.app import compute_aggregate_fields

# Fixed reference timestamp used by every test.
NOW = datetime(2026, 9, 12, 0, 0, 0, tzinfo=timezone.utc)


def make_document(**overrides):
    """A minimal DocumentsTable item."""

    document = {
        "document_id": "doc-001",
        "file_size": 500_000_000,
        "current_storage_class": "STANDARD",
        "upload_timestamp": "2026-01-01T00:00:00Z",
        "document_state": "ACTIVE",
    }

    document.update(overrides)

    return document


def make_event(days_ago, access_type="DOWNLOAD"):
    """An AccessHistoryTable event relative to NOW."""

    timestamp = (
        NOW - timedelta(days=days_ago)
    ).isoformat().replace("+00:00", "Z")

    return {
        "document_id": "doc-001",
        "access_timestamp": timestamp,
        "access_type": access_type,
    }


def test_zero_accesses():
    document = make_document()

    aggregate = compute_aggregate_fields(document, [], NOW)

    assert aggregate["access_count"] == 0
    assert float(aggregate["access_frequency"]) == 0.0
    assert aggregate["last_accessed_timestamp"] is None
    assert aggregate["days_since_last_access"] is None


def test_thirty_day_window_keeps_only_recent_events():
    document = make_document()

    events = [
        make_event(5),      # inside the window
        make_event(10),     # inside the window
        make_event(40),     # outside the window
        make_event(90),     # outside the window
    ]

    aggregate = compute_aggregate_fields(document, events, NOW)

    assert aggregate["access_count"] == 2


def test_access_frequency_for_12_events():
    document = make_document()

    events = [
        make_event(day)
        for day in range(0, 12)
    ]

    aggregate = compute_aggregate_fields(document, events, NOW)

    assert aggregate["access_count"] == 12
    assert aggregate["access_frequency"] == Decimal("0.4")
    assert float(aggregate["access_frequency"]) == 0.4


def test_newest_qualifying_event_is_last_accessed():
    document = make_document()

    older_event = make_event(20)
    newest_event = make_event(3)

    aggregate = compute_aggregate_fields(
        document,
        [older_event, newest_event],
        NOW
    )

    assert aggregate["last_accessed_timestamp"] == newest_event["access_timestamp"]


def test_days_since_last_access_uses_run_timestamp():
    document = make_document()

    aggregate = compute_aggregate_fields(
        document,
        [make_event(10)],
        NOW
    )

    days_since = aggregate["days_since_last_access"]

    assert isinstance(days_since, Decimal)
    assert float(days_since) == 10.0


def test_non_download_events_are_not_counted():
    document = make_document()

    events = [
        make_event(5, access_type="DOWNLOAD"),
        make_event(6, access_type="VIEW"),
        make_event(7, access_type="PREVIEW"),
    ]

    aggregate = compute_aggregate_fields(document, events, NOW)

    assert aggregate["access_count"] == 1
    assert float(aggregate["access_frequency"]) == 1 / 30


def test_file_size_mapped_and_cast_to_int():
    document = make_document(file_size=350_000)

    aggregate = compute_aggregate_fields(document, [], NOW)

    assert aggregate["file_size_bytes"] == 350_000
    assert isinstance(aggregate["file_size_bytes"], int)
    assert "file_size" not in aggregate


def test_document_state_passthrough():
    for state in ["ACTIVE", "CLOSED", "ARCHIVED"]:
        document = make_document(document_state=state)
        aggregate = compute_aggregate_fields(document, [], NOW)
        assert aggregate["document_state"] == state

    missing_state = make_document()
    del missing_state["document_state"]

    assert compute_aggregate_fields(missing_state, [], NOW)["document_state"] is None

    explicit_none = make_document(document_state=None)

    assert compute_aggregate_fields(explicit_none, [], NOW)["document_state"] is None


def test_dynamodb_number_representation():
    document = make_document()

    aggregate = compute_aggregate_fields(document, [make_event(10)], NOW)

    assert isinstance(aggregate["access_frequency"], Decimal)
    assert isinstance(aggregate["days_since_last_access"], Decimal)

    empty = compute_aggregate_fields(document, [], NOW)

    assert empty["days_since_last_access"] is None
    assert isinstance(empty["access_frequency"], Decimal)


def test_aggregation_timestamp_is_run_timestamp():
    document = make_document()

    aggregate = compute_aggregate_fields(document, [], NOW)

    assert aggregate["aggregation_timestamp"] == "2026-09-12T00:00:00Z"


def test_passthrough_fields_copied_from_document():
    document = make_document(
        document_id="doc-999",
        current_storage_class="STANDARD_IA",
        upload_timestamp="2025-06-15T12:30:00Z",
    )

    aggregate = compute_aggregate_fields(document, [], NOW)

    assert aggregate["document_id"] == "doc-999"
    assert aggregate["current_storage_class"] == "STANDARD_IA"
    assert aggregate["upload_timestamp"] == "2025-06-15T12:30:00Z"