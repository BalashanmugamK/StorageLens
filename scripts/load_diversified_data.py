"""
Load the validated DIVERSIFIED real corpus into the deployed
stack through the EXISTING Documents API, then apply the
controlled experimental metadata and access workload.

Methodology: real corpus + controlled experimental metadata and
access workload (same as the first real experiment); the
controlled variables (current_storage_class, document_state,
upload_timestamp / document age, access counts and recency)
come from data/real/diversified/workload_500.json with a fixed
seed.

Reuse (nothing re-implemented):
    - create_document_via_api / upload_pdf /
      validate_manifest_schema / is_api_document_id /
      save_mapping_to / resolve_api_url  (load_real_data)
    - resolve_stack_resources           (load_synthetic_data)
    - build_access_events               (generate_synthetic_data)

What it does:
    1. Creates each document through POST /documents and uploads
       the real PDF through the returned presigned PUT URL
       (identical to load_real_data.py).
    2. Then directly updates the DocumentsTable item with the
       controlled experimental metadata
       (current_storage_class, document_state, upload_timestamp).
       The existing API hard-codes STANDARD and never sets
       document_state, and these are experimental variables of
       this dataset by design -- the same controlled-write
       technique load_synthetic_data.py already uses for the
       synthetic corpus.
    3. Materializes DOWNLOAD access events with
       build_access_events (30-day model anchored now). The
       EXISTING access history is never purged: the events of the
       first real experiment and of the synthetic corpus stay
       untouched; this run only ever writes new document ids.
    4. Writes experiments/results/diversified_500_mapping.json
       with the same key structure as real_500_mapping.json so
       the unmodified run_real_optimizer.py can filter the
       diversified documents.

Resume support: per-document progress is persisted after every
document, and the access-event anchor keeps a re-run from
double-counting events.

Usage:
    python scripts/load_diversified_data.py
        [--stack-name intelligent-storage-cost-optimizer]
        [--region ap-south-1]
        [--limit N]     load only the first N documents
        [--skip-events] skip the access-event writes
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(Path(__file__).resolve().parent))

from load_real_data import (  # noqa: E402
    create_document_via_api,
    is_api_document_id,
    resolve_api_url,
    save_mapping_to,
    upload_pdf,
    validate_manifest_schema,
)
from load_synthetic_data import resolve_stack_resources  # noqa: E402
from generate_synthetic_data import build_access_events  # noqa: E402


WORKLOAD_FILE = (
    REPO_ROOT / "data" / "real" / "diversified"
    / "workload_500.json"
)
MAPPING_FILE = (
    REPO_ROOT / "experiments" / "results"
    / "diversified_500_mapping.json"
)


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Load the diversified real corpus with its "
            "controlled experimental workload."
        )
    )
    parser.add_argument(
        "--stack-name",
        default="intelligent-storage-cost-optimizer",
    )
    parser.add_argument(
        "--region",
        default="ap-south-1",
    )
    parser.add_argument(
        "--api-url",
        default=None,
        help=(
            "Documents API endpoint; resolved from the stack "
            "output DocumentsApiEndpoint when omitted."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Load only the first N documents.",
    )
    parser.add_argument(
        "--skip-events",
        action="store_true",
        help="Skip the access-event writes.",
    )
    args = parser.parse_args()

    import boto3

    manifest_raw = json.loads(
        (REPO_ROOT / "data" / "real" / "diversified"
         / "manifest.json").read_text(encoding="utf-8")
    )
    workload_raw = json.loads(
        WORKLOAD_FILE.read_text(encoding="utf-8")
    )

    manifest_documents = {
        document["document_id"]: document
        for document in manifest_raw["documents"]
    }
    workload = {
        record["document_id"]: record
        for record in workload_raw["documents"]
    }

    if set(manifest_documents) != set(workload):
        raise RuntimeError(
            "Manifest and workload document ids differ; "
            "regenerate both."
        )

    ordered_ids = [
        m["document_id"]
        for m in manifest_raw["documents"]
    ]
    ordered = [
        (manifest_documents[i], workload[i])
        for i in ordered_ids
    ]

    if args.limit:
        ordered = ordered[: args.limit]

    validate_manifest_schema(
        [doc for doc, _ in ordered]
    )

    existing_mapping = {}

    if MAPPING_FILE.exists():
        existing_mapping = json.loads(
            MAPPING_FILE.read_text(encoding="utf-8")
        )

    existing_by_manifest_id = {
        entry["manifest_document_id"]: entry
        for entry in existing_mapping.get("documents", [])
    }

    resources = resolve_stack_resources(
        args.stack_name, args.region
    )
    api_url = resolve_api_url(
        args.stack_name, args.region, args.api_url
    )

    print(f"Stack:   {args.stack_name}")
    print(f"Region:  {args.region}")
    print(f"Api:     {api_url}")
    print(f"Bucket:  {resources['bucket']}")
    print(
        f"Tables:  {resources['documents_table']}, "
        f"{resources['access_history_table']}"
    )

    dynamodb = boto3.resource(
        "dynamodb", region_name=args.region
    )

    documents_table = dynamodb.Table(
        resources["documents_table"]
    )
    access_history_table = dynamodb.Table(
        resources["access_history_table"]
    )

    pdf_dir = (
        REPO_ROOT / "data" / "real" / "diversified" / "pdfs"
    )

    entries = []

    for number, (document, controlled) in enumerate(
        ordered, start=1
    ):
        manifest_document_id = document["document_id"]

        existing = existing_by_manifest_id.get(
            manifest_document_id
        )

        if existing and is_api_document_id(
            existing["api_document_id"]
        ):
            api_document_id = existing["api_document_id"]
            object_key = existing["object_key"]
            print(
                f"[{number}] {manifest_document_id} already "
                f"loaded (resume), reusing {api_document_id}."
            )
        else:
            api_document_id, created = create_document_via_api(
                api_url, document
            )
            object_key = created["object_key"]

            pdf_path = pdf_dir / document["file_name"]

            print(
                f"[{number}] {manifest_document_id} -> "
                f"{api_document_id} uploading "
                f"{document['file_size_bytes']} bytes ..."
            )

            upload_pdf(
                created["upload_url"],
                pdf_path,
                document["content_type"],
            )

        # Controlled experimental metadata: written directly to
        # the DocumentsTable item (the API hard-codes STANDARD
        # and never sets document_state).
        expression_attribute_names = {
            "#current_storage_class": (
                "current_storage_class"
            ),
            "#upload_timestamp": "upload_timestamp",
            "#document_state": "document_state",
        }

        expression_attribute_values = {
            ":current_storage_class": (
                controlled["current_storage_class"]
            ),
            ":upload_timestamp": (
                controlled["upload_timestamp"]
            ),
        }

        set_parts = [
            "#current_storage_class = :current_storage_class",
            "#upload_timestamp = :upload_timestamp",
        ]

        remove_parts = []

        if controlled["document_state"] is None:
            # State not yet known is a MISSING attribute, exactly
            # like load_synthetic_data.py writes it.
            remove_parts.append("#document_state")
        else:
            expression_attribute_values[
                ":document_state"
            ] = controlled["document_state"]

            set_parts.append(
                "#document_state = :document_state"
            )

        update_expression = "SET " + ", ".join(set_parts)

        if remove_parts:
            update_expression += (
                " REMOVE " + ", ".join(remove_parts)
            )

        documents_table.update_item(
            Key={"document_id": api_document_id},
            UpdateExpression=update_expression,
            ExpressionAttributeNames=(
                expression_attribute_names
            ),
            ExpressionAttributeValues=(
                expression_attribute_values
            ),
        )

        entry = {
            "manifest_document_id": manifest_document_id,
            "granule_id": manifest_document_id,
            "package_id": document["package_id"],
            "file_name": document["file_name"],
            "file_size_bytes": document["file_size_bytes"],
            "sha256": document["sha256"],
            "collection": document["collection"],
            "document_type": document["document_type"],
            "jurisdiction": document["jurisdiction"],
            "court": document.get("court", ""),
            "agency": document.get("agency", ""),
            "date_issued": document["date_issued"],
            "api_document_id": api_document_id,
            "object_key": object_key,
            # Controlled experimental variables.
            "current_storage_class": (
                controlled["current_storage_class"]
            ),
            "document_state": controlled["document_state"],
            "upload_timestamp": (
                controlled["upload_timestamp"]
            ),
            "access_band": controlled["access_band"],
            "recency_band": controlled["recency_band"],
            "age_band": controlled["age_band"],
            "workload_access_count": (
                controlled["access_count"]
            ),
            "workload_days_since_last_access": (
                controlled["days_since_last_access"]
            ),
        }

        if existing:
            entry["current_storage_class"] = (
                existing.get(
                    "current_storage_class",
                    entry["current_storage_class"],
                )
            )
            entry["document_state"] = existing.get(
                "document_state", entry["document_state"]
            )
            entry["upload_timestamp"] = existing.get(
                "upload_timestamp", entry["upload_timestamp"]
            )

        entries.append(entry)

        # Persist progress after every document so an interrupted
        # run resumes without duplicates.
        save_mapping_to(
            MAPPING_FILE,
            {
                "dataset": "diversified",
                "source_dataset": (
                    "data/real/diversified/manifest.json"
                ),
                "methodology": (
                    "Real legal-document corpus + controlled "
                    "experimental metadata and access workload"
                ),
                "access_workload_from": str(
                    WORKLOAD_FILE.relative_to(REPO_ROOT)
                ),
                "access_event_anchor": (
                    existing_mapping.get(
                        "access_event_anchor"
                    )
                ),
                "document_count": len(entries),
                "event_count": (
                    existing_mapping.get("event_count")
                ),
                "documents": entries,
            },
        )

        if number % 50 == 0:
            print(f"  processed {number} documents ...")

    event_anchor = None
    event_count = existing_mapping.get("event_count")

    if args.skip_events:
        print("--skip-events set: no access events written.")
    elif existing_mapping.get("access_event_anchor"):
        print(
            "Access events were already written (anchor "
            f"{existing_mapping['access_event_anchor']}); "
            "skipping to avoid double-counting."
        )
    else:
        event_anchor = datetime.now(timezone.utc)

        print(
            "Writing controlled access events (existing "
            "30-day event model) ..."
        )

        with access_history_table.batch_writer() as batch:
            for entry in entries:
                events = build_access_events(
                    {
                        "document_id": entry[
                            "api_document_id"
                        ],
                        "access_count": entry[
                            "workload_access_count"
                        ],
                        "days_since_last_access": entry[
                            "workload_days_since_last_access"
                        ],
                    },
                    event_anchor,
                )

                for event in events:
                    batch.put_item(Item=event)

        event_count = sum(
            entry["workload_access_count"]
            for entry in entries
        )

    if event_anchor is not None:
        save_mapping_to(
            MAPPING_FILE,
            {
                "dataset": "diversified",
                "source_dataset": (
                    "data/real/diversified/manifest.json"
                ),
                "methodology": (
                    "Real legal-document corpus + controlled "
                    "experimental metadata and access workload"
                ),
                "access_workload_from": str(
                    WORKLOAD_FILE.relative_to(REPO_ROOT)
                ),
                "access_event_anchor": (
                    event_anchor.isoformat()
                ),
                "document_count": len(entries),
                "event_count": event_count,
                "documents": entries,
            },
        )

    print("Load complete.")
    print(
        f"Created/verified {len(entries)} diversified "
        f"documents; access events on record: {event_count}."
    )
    print(f"Mapping: {MAPPING_FILE}")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)