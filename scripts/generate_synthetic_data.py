"""
Generate the synthetic live-pipeline dataset used to test the
deployed Intelligent Cloud Storage Cost Optimizer.

Input:  data/synthetic/workload_500.json
        (the existing seeded 500-document aggregate workload)

Output: data/synthetic/documents_500.json
        DocumentsTable-shaped records (object keys, file names,
        content types, legal metadata) — one row per document.

        data/synthetic/access_events_500.json
        AccessHistoryTable-shaped raw DOWNLOAD events materialized
        inside the 30-day observation window, consistent with each
        document's seeded access_count / days_since_last_access.

Everything is deterministic: the aggregate source is a fixed-seed
workload and the derived naming/metadata uses its own fixed seed,
so regenerating always produces identical files.

No real personal data is used anywhere.
"""

import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]

WORKLOAD_FILE = REPO_ROOT / "data" / "synthetic" / "workload_500.json"
DOCUMENTS_FILE = REPO_ROOT / "data" / "synthetic" / "documents_500.json"
EVENTS_FILE = REPO_ROOT / "data" / "synthetic" / "access_events_500.json"

OBSERVATION_PERIOD_DAYS = 30
SECONDS_PER_DAY = 86400
DERIVED_DATA_SEED = 20260913

# Events older than the newest one are spread over half of the
# remaining room up to a hard one-day margin before the 30-day
# cutoff, so every generated event stays safely inside a live
# 30-day window for at least ~12 hours after generation.
SPAN_FRACTION = 0.5


# Synthetic legal-document catalogue: (category, extension,
# content_type). No real documents or personal data.
LEGAL_DOCUMENT_TYPES = [
    ("contract", "pdf", "application/pdf"),
    ("litigation-brief", "pdf", "application/pdf"),
    ("court-filing", "pdf", "application/pdf"),
    ("affidavit", "pdf", "application/pdf"),
    ("nda", "pdf", "application/pdf"),
    ("client-memo", "docx",
     "application/vnd.openxmlformats-officedocument"
     ".wordprocessingml.document"),
    ("case-summary", "txt", "text/plain"),
]

# Synthetic practice areas used as category metadata.
PRACTICE_AREAS = [
    "litigation",
    "corporate",
    "real-estate",
    "family",
    "intellectual-property",
    "employment",
]


def parse_reference_timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def format_timestamp(value):
    """Format matching the documents Lambda writer (+00:00)."""

    return (
        value.replace(microsecond=0).isoformat()
    )


def build_document_metadata(
    aggregate,
    document_number,
    rng,
    reference_time,
):
    """
    Derive live-pipeline document metadata from one seeded
    aggregate record.
    """

    category, extension, content_type = rng.choice(
        LEGAL_DOCUMENT_TYPES
    )

    practice_area = rng.choice(PRACTICE_AREAS)

    file_name = (
        f"{category}"
        f"_{practice_area}"
        f"_{document_number:04d}"
        f".{extension}"
    )

    object_key = (
        f"documents/{aggregate['document_id']}/{file_name}"
    )

    return {
        "document_id": aggregate["document_id"],
        "object_key": object_key,
        "file_name": file_name,
        "content_type": content_type,
        "file_size": int(aggregate["file_size_bytes"]),
        "upload_timestamp": aggregate["upload_timestamp"],
        "current_storage_class": (
            aggregate["current_storage_class"]
        ),
        "document_state": aggregate["document_state"],
        "practice_area": practice_area,
        "owner": f"ATTORNEY-{document_number:04d}",
    }


def build_access_events(
    aggregate,
    event_reference_time,
):
    """
    Materialize raw DOWNLOAD events inside a 30-day
    observation window anchored at event_reference_time.

    Events are anchored at generation time (not the static
    workload reference timestamp) so all of them always fall
    inside a live 30-day window whenever the aggregator runs
    later. The newest event lands exactly at
    (event_reference_time - days_since_last_access) so an
    aggregator run reproduces the seeded aggregate values.

    Older events are spread over half of the remaining room up
    to a one-day safety margin before the 30-day cutoff, so no
    generated event ever sits at the window boundary where it
    could age out between generation and aggregation.
    """

    access_count = aggregate["access_count"]

    if access_count == 0:
        return []

    days_since_last_access = (
        aggregate["days_since_last_access"]
    )

    room = max(
        0.0,
        (OBSERVATION_PERIOD_DAYS - 1) - days_since_last_access,
    )

    span_days = SPAN_FRACTION * room

    if access_count == 1:
        # Only the newest event exists.
        offsets_days = [days_since_last_access]
    elif span_days * SECONDS_PER_DAY >= 2 * access_count:
        # Enough room for distinct even spacing across the
        # span: index 0 is the newest event, the last one is
        # the oldest.
        offsets_days = [
            days_since_last_access
            + span_days * (index / (access_count - 1))
            for index in range(access_count)
        ]
    else:
        # Near the boundary the span cannot hold distinct
        # second-level timestamps: make each additional event
        # one second older than the newest instead.
        offsets_days = [
            days_since_last_access
            + index / SECONDS_PER_DAY
            for index in range(access_count)
        ]

    events = []

    for offset_days in offsets_days:
        event_time = event_reference_time - timedelta(
            days=offset_days
        )

        events.append(
            {
                "document_id": aggregate["document_id"],
                "access_timestamp": format_timestamp(
                    event_time
                ),
                "access_type": "DOWNLOAD",
            }
        )

    # Every pair of event timestamps differs by at least one
    # second in either branch.
    return events


def main():
    with WORKLOAD_FILE.open("r", encoding="utf-8") as file:
        workload = json.load(file)

    documents = workload["documents"]

    reference_time = parse_reference_timestamp(
        workload["reference_timestamp"]
    )

    rng = random.Random(DERIVED_DATA_SEED)

    # Access events are anchored at generation time so the
    # whole event set stays inside a live 30-day window.
    event_reference_time = datetime.now(timezone.utc)

    document_records = []
    event_records = []

    for number, aggregate in enumerate(
        documents,
        start=1,
    ):
        document_records.append(
            build_document_metadata(
                aggregate,
                number,
                rng,
                reference_time,
            )
        )

        event_records.extend(
            build_access_events(
                aggregate,
                event_reference_time,
            )
        )

    # Metadata references the workload by repo-relative path, not
    # an absolute machine path, so regenerated files are portable.
    relative_source = (
        "data/synthetic/" + WORKLOAD_FILE.name
    )

    DOCUMENTS_FILE.write_text(
        json.dumps(
            {
                "source_workload": relative_source,
                "reference_timestamp": (
                    workload["reference_timestamp"]
                ),
                "document_count": len(document_records),
                "documents": document_records,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    EVENTS_FILE.write_text(
        json.dumps(
            {
                "source_workload": relative_source,
                "reference_timestamp": (
                    workload["reference_timestamp"]
                ),
                "observation_period_days": (
                    OBSERVATION_PERIOD_DAYS
                ),
                "event_count": len(event_records),
                "events": event_records,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    print(
        f"Generated {len(document_records)} synthetic "
        f"document records."
    )
    print(
        f"Generated {len(event_records)} synthetic "
        f"access events."
    )
    print(f"Documents: {DOCUMENTS_FILE}")
    print(f"Events:    {EVENTS_FILE}")


if __name__ == "__main__":
    main()