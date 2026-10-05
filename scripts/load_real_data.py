"""
Load the validated real USCOURTS corpus into the deployed stack
through the EXISTING Documents API. Dataset-agnostic: point
--dataset-dir at data/real/<dir>/ for any public legal dataset
whose manifest.json carries file_name, file_size_bytes and
content_type.

Prerequisites:
    data/real/us_courts/pdfs/*.pdf (verified against manifest.json)
    The CloudFormation/SAM stack must already be deployed.

What it does (same live pipeline shape as load_synthetic_data.py):
    1. Resolves resource names from the stack's outputs —
       nothing is hard-coded.
    2. Creates 500 documents through the existing
       POST /documents endpoint and uploads each real PDF to S3
       through the returned presigned PUT URL. Metadata from the
       manifest is preserved where the existing API supports it
       (file_name, file_size, content_type).
    3. Writes back-dated DOWNLOAD events to AccessHistoryTable,
       reusing the SAME access-frequency distribution already
       used by the synthetic experiment (workload_500.json) and
       the same event materialization logic
       (generate_synthetic_data.build_access_events).
    4. Saves the manifest -> API mapping so the optimizer run can
       filter the real documents and keep provenance.

Existing synthetic data in the stack is left untouched; the
optimizer run filters real documents by API-issued UUID ids.

Upload progress is persisted after every document so a partially
interrupted run resumes instead of producing duplicates, and the
access-event write is anchored once in the mapping so a re-run
cannot double-count events.

Usage:
    python scripts/load_real_data.py
        [--stack-name intelligent-storage-cost-optimizer]
        [--region ap-south-1]
        [--dataset-dir us_courts]
        [--workload-file workload_500.json]
        [--limit N]        load only the first N documents
        [--skip-events]    skip the access-event writes
"""

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests


REPO_ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(Path(__file__).resolve().parent))

from load_synthetic_data import resolve_stack_resources
from generate_synthetic_data import build_access_events

def resolve_dataset_paths(dataset_dir, workload_file, mapping_file):
    """
    Per-dataset paths. The default dataset_dir ("us_courts")
    keeps the original mapping/result filename conventions so the
    Phase B artifacts stay stable; any other public legal dataset
    lives in its own data/real/<dataset_dir>/ directory with its
    own mapping file.
    """

    dataset_root = REPO_ROOT / "data" / "real" / dataset_dir

    if workload_file:
        workload = (
            REPO_ROOT / "data" / "synthetic" / workload_file
        )
    else:
        workload = None

    if mapping_file:
        mapping = (
            REPO_ROOT / "experiments" / "results" / mapping_file
        )
    elif dataset_dir == "us_courts":
        mapping = (
            REPO_ROOT / "experiments" / "results"
            / "real_500_mapping.json"
        )
    else:
        mapping = (
            REPO_ROOT / "experiments" / "results"
            / f"real_{dataset_dir}_mapping.json"
        )

    return {
        "manifest": dataset_root / "manifest.json",
        "pdf_dir": dataset_root / "pdfs",
        "workload": workload,
        "mapping": mapping,
    }

API_TIMEOUT_SECONDS = 30

# Transient network failures (connection resets, timeouts) are
# retried in place; on a persistent failure the run stops and
# resume continues from the mapping file.
TRANSIENT_ATTEMPTS = 3
TRANSIENT_PAUSE_SECONDS = 3


def request_with_retries(method, *args, **kwargs):
    import time

    for attempt in range(1, TRANSIENT_ATTEMPTS + 1):
        try:
            return requests.request(method, *args, **kwargs)
        except (requests.ConnectionError, requests.Timeout):
            if attempt == TRANSIENT_ATTEMPTS:
                raise

            time.sleep(TRANSIENT_PAUSE_SECONDS)


def is_api_document_id(document_id):
    """API-created documents get lower-case UUID ids."""

    try:
        uuid.UUID(document_id)
    except ValueError:
        return False

    return document_id == document_id.lower()


def resolve_api_url(stack_name, region, args_api_url):
    """
    Resolve the Documents API endpoint from the stack's
    DocumentsApiEndpoint output.
    """

    if args_api_url:
        return args_api_url.rstrip("/")

    import boto3

    outputs = (
        boto3.client("cloudformation", region_name=region)
        .describe_stacks(StackName=stack_name)
        .get("Stacks", [])[0]
        .get("Outputs", [])
    )

    api_url = next(
        (
            output["OutputValue"]
            for output in outputs
            if output["OutputKey"] == "DocumentsApiEndpoint"
        ),
        None,
    )

    if not api_url:
        raise RuntimeError(
            "Stack output DocumentsApiEndpoint not found."
        )

    return api_url


def create_document_via_api(api_url, document):
    """
    Create one document through the existing POST /documents
    endpoint.

    Manifest metadata is preserved where the API supports it:
    file_name, file_size, content_type.
    """

    import boto3

    body = {
        "file_name": document["file_name"],
        "file_size": document["file_size_bytes"],
        "content_type": document["content_type"],
    }

    http = request_with_retries("POST",
        f"{api_url}/documents",
        json=body,
        timeout=API_TIMEOUT_SECONDS,
    )

    if http.status_code != 201:
        raise RuntimeError(
            f"POST /documents returned "
            f"{http.status_code}: {http.text}"
        )

    created = http.json()

    return created["document_id"], created


def upload_pdf(upload_url, pdf_path, content_type):
    """Upload the real PDF through the presigned PUT URL."""

    if not pdf_path.exists():
        raise RuntimeError(f"Missing PDF: {pdf_path}")

    http = request_with_retries("PUT",
        upload_url,
        data=pdf_path.read_bytes(),
        headers={"Content-Type": content_type},
        timeout=API_TIMEOUT_SECONDS * 4,
    )

    if http.status_code != 200:
        raise RuntimeError(
            f"presigned PUT returned "
            f"{http.status_code}: {http.text}"
        )


REQUIRED_MANIFEST_FIELDS = (
    "document_id",
    "file_name",
    "file_size_bytes",
    "content_type",
)


def validate_manifest_schema(documents):
    """
    The loader is dataset-agnostic: any public legal dataset works
    as long as every manifest entry carries the fields the
    existing Documents API consumes. Fail clearly before any AWS
    call when a dataset's manifest does not conform.
    """

    problems = []

    for number, document in enumerate(documents, start=1):
        for field in REQUIRED_MANIFEST_FIELDS:
            if field not in document:
                problems.append(
                    f"document[{number}] "
                    f"({document.get('file_name', '?')}): "
                    f"missing required field '{field}'"
                )

        size = document.get("file_size_bytes")

        if size is not None and (not isinstance(size, int) or size <= 0):
            problems.append(
                f"document[{number}]: file_size_bytes must be a "
                f"positive integer, got {size!r}"
            )

    if problems:
        raise RuntimeError(
            "Manifest does not conform to the loader schema "
            f"({', '.join(REQUIRED_MANIFEST_FIELDS)} "
            "required per entry):\n"
            + "\n".join(problems[:20])
        )


def save_mapping_to(mapping_path, mapping):
    mapping_path.parent.mkdir(
        parents=True, exist_ok=True
    )

    mapping_path.write_text(
        json.dumps(mapping, indent=2),
        encoding="utf-8",
    )


def purge_access_history(access_history_table):
    """
    Delete all existing events so re-seeding cannot
    double-count (same loop as load_synthetic_data.py).
    """

    deleted = 0

    print("Purging existing access-history events ...")

    while True:
        old_events = access_history_table.scan().get(
            "Items", []
        )

        if not old_events:
            break

        keys = [
            {
                "document_id": event["document_id"],
                "access_timestamp": event["access_timestamp"],
            }
            for event in old_events
        ]

        with access_history_table.batch_writer() as batch:
            for key in keys:
                batch.delete_item(Key=key)

        deleted += len(keys)

    print(f"Purged {deleted} existing events.")


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Load the real USCOURTS corpus through the "
            "existing Documents API."
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
        "--dataset-dir",
        default="us_courts",
        help=(
            "Subdirectory of data/real/ holding manifest.json "
            "and pdfs/ for the dataset to load."
        ),
    )

    parser.add_argument(
        "--workload-file",
        default="workload_500.json",
        help=(
            "File under data/synthetic/ whose access-frequency "
            "distribution is reused by position."
        ),
    )

    parser.add_argument(
        "--mapping-file",
        default=None,
        help=(
            "Mapping file under experiments/results/. Defaults "
            "to real_500_mapping.json for the us_courts dataset "
            "and real_<dataset_dir>_mapping.json otherwise."
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

    parser.add_argument(
        "--purge-access-history",
        action="store_true",
        help=(
            "Delete all existing events in AccessHistoryTable "
            "before writing, so re-seeding cannot double-count."
        ),
    )

    args = parser.parse_args()

    import boto3

    paths = resolve_dataset_paths(
        args.dataset_dir,
        args.workload_file,
        args.mapping_file,
    )

    manifest_raw = json.loads(
        paths["manifest"].read_text(encoding="utf-8")
    )

    if paths["workload"] is None:
        raise RuntimeError("No workload file resolved.")

    workload_raw = json.loads(
        paths["workload"].read_text(encoding="utf-8")
    )

    documents = manifest_raw["documents"]
    workload = workload_raw["documents"]

    if args.limit:
        documents = documents[: args.limit]

    validate_manifest_schema(documents)

    if len(workload) < len(documents):
        raise RuntimeError(
            "Access-frequency workload is smaller than the "
            "document set."
        )

    existing_mapping = {}

    if paths["mapping"].exists():
        existing_mapping = json.loads(
            paths["mapping"].read_text(encoding="utf-8")
        )

    # The manifest's own document_id is the generic resume key
    # (for USCOURTS it equals the granule_id).
    existing_by_manifest_id = {
        entry["manifest_document_id"]: entry
        for entry in existing_mapping.get("documents", [])
    }

    resources = resolve_stack_resources(
        args.stack_name,
        args.region,
    )
    api_url = resolve_api_url(
        args.stack_name,
        args.region,
        args.api_url,
    )

    print(f"Stack:   {args.stack_name}")
    print(f"Region:  {args.region}")
    print(f"Api:     {api_url}")
    print(f"Bucket:  {resources['bucket']}")
    print(f"Tables:  {resources['documents_table']}, "
          f"{resources['access_history_table']}")

    dynamodb = boto3.resource(
        "dynamodb", region_name=args.region
    )

    access_history_table = dynamodb.Table(
        resources["access_history_table"]
    )

    if args.purge_access_history:
        purge_access_history(access_history_table)

    entries = []

    for number, document in enumerate(
        documents,
        start=1,
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
                f"[{number}] {manifest_document_id} "
                f"already loaded (resume), reusing "
                f"{api_document_id}."
            )
        else:
            api_document_id, created = create_document_via_api(
                api_url,
                document,
            )
            object_key = created["object_key"]

            pdf_path = paths["pdf_dir"] / document["file_name"]

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

        entries.append(
            {
                "manifest_document_id": manifest_document_id,
                # Identity/provenance fields vary between public
                # legal datasets; only document_id is required.
                "granule_id": document.get(
                    "granule_id", manifest_document_id
                ),
                "package_id": document.get("package_id", ""),
                "file_name": document["file_name"],
                "file_size_bytes": document["file_size_bytes"],
                "sha256": document.get("sha256", ""),
                "court": document.get("court", ""),
                "date_issued": document.get(
                    "date_issued", ""
                ),
                "api_document_id": api_document_id,
                "object_key": object_key,
                "current_storage_class": "STANDARD",
                "document_state": None,
                # The access-frequency distribution is reused
                # from the synthetic experiment by position.
                "workload_access_count": workload[
                    number - 1
                ]["access_count"],
                "workload_days_since_last_access": (
                    workload[number - 1][
                        "days_since_last_access"
                    ]
                ),
            }
        )

        # Persist progress after every document so an
        # interrupted run resumes without duplicates.
        save_mapping_to(
            paths["mapping"],
            {
                "source_dataset": str(
                    paths["manifest"].relative_to(REPO_ROOT)
                ),
                "access_distribution_reused_from": (
                    "data/synthetic/workload_500.json"
                ),
                "access_event_anchor": existing_mapping.get(
                    "access_event_anchor"
                ),
                "document_count": len(entries),
                "event_count": existing_mapping.get(
                    "event_count"
                ),
                "documents": entries,
            },
        )

        if number % 50 == 0:
            print(f"  uploaded {number} documents ...")

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
            "Writing access events (reusing the synthetic "
            "access-frequency distribution) ..."
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

    # The anchor marks the completed event write in the mapping
    # so a later re-run skips it instead of double-counting.
    if event_anchor is not None:
        save_mapping_to(
            paths["mapping"],
            {
                "source_dataset": str(
                    paths["manifest"].relative_to(REPO_ROOT)
                ),
                "access_distribution_reused_from": (
                    "data/synthetic/workload_500.json"
                ),
                "access_event_anchor": event_anchor.isoformat(),
                "document_count": len(entries),
                "event_count": event_count,
                "documents": entries,
            },
        )

    print("Load complete.")
    print(
        f"Created/verified {len(entries)} real documents; "
        f"access events on record: {event_count}."
    )
    print(f"Mapping: {paths['mapping']}")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)