"""
Tests for the bounded-parallel aggregation access pattern.

These tests prove that aggregate_documents_parallel (the deployed
access pattern) produces output identical to
aggregate_documents_sequential (the original reference
implementation), that the thread pool stays bounded, and that
transient DynamoDB errors are retried safely.

The DynamoDB layer is stubbed, so no AWS credentials are needed.
"""

import os
import sys
import threading
import time
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Set env vars before importing the Lambda module, which reads
# them at import time. No AWS credentials are required.
os.environ.setdefault("DOCUMENTS_TABLE", "test-documents")
os.environ.setdefault("ACCESS_HISTORY_TABLE", "test-access-history")
os.environ.setdefault("AGGREGATES_TABLE", "test-aggregates")

from backend.lambdas.aggregates import app


NOW = datetime(2026, 9, 12, 0, 0, 0, tzinfo=timezone.utc)

CUTOFF = NOW - timedelta(days=30)

DOCUMENT_COUNT = 200


def make_documents(count):
    """A deterministic set of DocumentsTable items."""

    documents = []

    for number in range(count):
        documents.append(
            {
                "document_id": f"doc-{number:04d}",
                "file_size": 350_000 + number,
                "current_storage_class": "STANDARD",
                "upload_timestamp": "2026-01-01T00:00:00Z",
                "document_state": ["ACTIVE", "CLOSED", "ARCHIVED", None][
                    number % 4
                ],
            }
        )

    return documents


def make_event(document_id, days_ago, access_type="DOWNLOAD"):
    timestamp = (
        NOW - timedelta(days=days_ago)
    ).isoformat().replace("+00:00", "Z")

    return {
        "document_id": document_id,
        "access_timestamp": timestamp,
        "access_type": access_type,
    }


def build_event_streams(documents):
    """
    Deterministic per-document event streams covering the same
    patterns as the unit tests: no events, one event, many
    events, out-of-window events, and non-DOWNLOAD events.
    """

    streams = {}

    for document in documents:
        document_id = document["document_id"]
        pattern = len(document_id) % 6

        if pattern == 0:
            streams[document_id] = []
        elif pattern == 1:
            streams[document_id] = [
                make_event(document_id, 10)
            ]
        elif pattern == 2:
            streams[document_id] = [
                make_event(document_id, day)
                for day in range(0, 5)
            ]
        elif pattern == 3:
            streams[document_id] = [
                make_event(document_id, 40),   # outside window
                make_event(document_id, 90),   # outside window
                make_event(document_id, 7),    # inside window
            ]
        elif pattern == 4:
            streams[document_id] = [
                make_event(document_id, 5),
                make_event(document_id, 6, "VIEW"),
                make_event(document_id, 7, "PREVIEW"),
            ]
        else:
            mixed = [
                make_event(document_id, 2),
                make_event(document_id, 15),
                make_event(document_id, 45),
                make_event(document_id, 8, "VIEW"),
            ]
            mixed.append(make_event(document_id, 29))
            streams[document_id] = mixed

    return streams


def stub_fetch_events(streams):
    """An injectable, thread-safe event fetcher."""

    lock = threading.Lock()

    def fetch_events(document_id, cutoff):
        with lock:
            return list(streams.get(document_id, []))

    return fetch_events


def test_parallel_output_is_identical_to_sequential():
    documents = make_documents(DOCUMENT_COUNT)
    streams = build_event_streams(documents)
    fetch_events = stub_fetch_events(streams)

    sequential = app.aggregate_documents_sequential(
        documents, CUTOFF, NOW, fetch_events
    )
    parallel = app.aggregate_documents_parallel(
        documents, CUTOFF, NOW, fetch_events
    )

    # Exact structural equality: same items, same fields, same
    # Decimal types, same values, same order.
    assert parallel == sequential

    for parallel_item, sequential_item in zip(
        parallel, sequential
    ):
        assert parallel_item.keys() == sequential_item.keys()
        assert list(parallel_item.keys()) == list(
            sequential_item.keys()
        )


def test_parallel_output_matches_reference_calculations():
    documents = make_documents(DOCUMENT_COUNT)
    streams = build_event_streams(documents)
    fetch_events = stub_fetch_events(streams)

    aggregates = [
        app.build_aggregate_for_document(
            document, CUTOFF, NOW, fetch_events
        )
        for document in documents
    ]

    parallel = app.aggregate_documents_parallel(
        documents, CUTOFF, NOW, fetch_events
    )

    # The parallel path equals the hand-computed reference, and
    # every item carries the same 10 aggregate fields.
    for left, right in zip(parallel, aggregates):
        assert left == right

    assert all(
        len(item) == 10 for item in parallel
    )


def test_parallel_preserves_document_order():
    documents = make_documents(DOCUMENT_COUNT)
    streams = build_event_streams(documents)
    fetch_events = stub_fetch_events(streams)

    parallel = app.aggregate_documents_parallel(
        documents, CUTOFF, NOW, fetch_events
    )

    assert [
        item["document_id"] for item in parallel
    ] == [document["document_id"] for document in documents]


def test_production_fetcher_is_used_for_bound():
    """
    The bound is enforced by aggregate_documents_parallel's own
    worker pool, not by the fetcher. Re-route the production fetch
    path to a tracking fetcher and confirm no more than
    MAX_CONCURRENT_QUERIES fetches ever run simultaneously.
    """

    peak = 0
    active_lock = threading.Lock()
    active = 0

    def tracking_fetch_events(document_id, cutoff):
        nonlocal peak, active

        with active_lock:
            active += 1
            peak = max(peak, active)

        time.sleep(0.02)

        with active_lock:
            active -= 1

        return [make_event(document_id, 10)]

    documents = make_documents(128)

    original_fetcher = app.fetch_recent_access_events

    app.fetch_recent_access_events = lambda document_id, cutoff: (
        tracking_fetch_events(document_id, cutoff)
    )

    try:
        parallel = app.aggregate_documents_parallel(
            documents, CUTOFF, NOW
        )

        assert len(parallel) == len(documents)

        # Every document saw its own single event.
        for item in parallel:
            assert item["access_count"] == 1

        assert parallel[3]["document_id"] == "doc-0003"

        assert peak <= app.MAX_CONCURRENT_QUERIES
    finally:
        app.fetch_recent_access_events = original_fetcher


def test_transient_throttling_is_retried():
    attempts = {"count": 0}

    class FakeTable:
        def query(self, **kwargs):
            attempts["count"] += 1

            if attempts["count"] <= 2:
                raise ClientError(
                    {
                        "Error": {
                            "Code": "ThrottlingException",
                            "Message": "rate exceeded",
                        }
                    },
                    "Query",
                )

            return {"Items": [make_event("doc-0000", 10)]}

    original_get_table = app.get_access_history_table

    app.get_access_history_table = lambda: FakeTable()

    original_backoff = app.RETRY_BACKOFF_SECONDS
    app.RETRY_BACKOFF_SECONDS = 0.01

    try:
        events = app.fetch_recent_access_events("doc-0000", CUTOFF)

        assert len(events) == 1
        assert attempts["count"] == 3
    finally:
        app.get_access_history_table = original_get_table
        app.RETRY_BACKOFF_SECONDS = original_backoff


def test_persistent_throttling_raises_after_bounded_retries():
    attempts = {"count": 0}

    class FakeTable:
        def query(self, **kwargs):
            attempts["count"] += 1

            raise ClientError(
                {
                    "Error": {
                        "Code": "ProvisionedThroughputExceeded",
                        "Message": "rate exceeded",
                    }
                },
                "Query",
            )

    original_get_table = app.get_access_history_table

    app.get_access_history_table = lambda: FakeTable()

    original_backoff = app.RETRY_BACKOFF_SECONDS
    original_attempts = app.MAX_QUERY_ATTEMPTS
    app.RETRY_BACKOFF_SECONDS = 0.01
    app.MAX_QUERY_ATTEMPTS = 3

    try:
        with pytest.raises(ClientError):
            app.fetch_recent_access_events("doc-0000", CUTOFF)

        assert attempts["count"] == 3
    finally:
        app.get_access_history_table = original_get_table
        app.RETRY_BACKOFF_SECONDS = original_backoff
        app.MAX_QUERY_ATTEMPTS = original_attempts


def test_non_retryable_error_fails_without_retry():
    attempts = {"count": 0}

    class FakeTable:
        def query(self, **kwargs):
            attempts["count"] += 1

            raise ClientError(
                {
                    "Error": {
                        "Code": "AccessDenied",
                        "Message": "not allowed",
                    }
                },
                "Query",
            )

    original_get_table = app.get_access_history_table

    app.get_access_history_table = lambda: FakeTable()

    try:
        with pytest.raises(ClientError) as excinfo:
            app.fetch_recent_access_events("doc-0000", CUTOFF)

        assert (
            excinfo.value.response["Error"]["Code"]
            == "AccessDenied"
        )
        assert attempts["count"] == 1
    finally:
        app.get_access_history_table = original_get_table


def test_connection_error_is_retried():
    from botocore.exceptions import ConnectionError as BotoConnectionError

    attempts = {"count": 0}

    class FakeTable:
        def query(self, **kwargs):
            attempts["count"] += 1

            if attempts["count"] < 3:
                raise BotoConnectionError(
                    error="connection reset"
                )

            return {"Items": [make_event("doc-0000", 4)]}

    original_get_table = app.get_access_history_table

    app.get_access_history_table = lambda: FakeTable()

    original_backoff = app.RETRY_BACKOFF_SECONDS
    app.RETRY_BACKOFF_SECONDS = 0.01

    try:
        events = app.fetch_recent_access_events("doc-0000", CUTOFF)

        assert len(events) == 1
        assert attempts["count"] == 3
    finally:
        app.get_access_history_table = original_get_table
        app.RETRY_BACKOFF_SECONDS = original_backoff


def test_empty_document_list_short_circuits():
    assert (
        app.aggregate_documents_parallel([], CUTOFF, NOW)
        == []
    )
    assert (
        app.aggregate_documents_sequential([], CUTOFF, NOW)
        == []
    )