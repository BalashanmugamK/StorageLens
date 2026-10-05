"""
Full validation report for the diversified real corpus and its
controlled workload (Phase V-2).

Validates corpus (manifest/files/hashes), the controlled workload
distributions, and -- through the EXISTING pipeline code only --
the optimizer's input space and decision coverage:

    1-15. the required corpus validation checks
    16.   current_storage_class distribution
    17.   document_state distribution
    18.   access_count histogram / band distribution
    19.   access_frequency distribution
    20.   days_since_last_access distribution
    21.   document age distribution
    22.   eligible storage-class distribution (STATE_ELIGIBILITY)
    23.   LOCAL DRY-RUN of the existing aggregation +
          adapter + OptimizerInput schema validation +
          optimize_document (decision-scenario coverage)

The dry-run materializes the access events with the existing
build_access_events and aggregates them with the existing
compute_aggregate_fields (the AggregatesFunction's pure helper),
so the inputs the live run will see are exercised end-to-end
locally. The dry run never writes AWS results: the live results
come from the real AggregatesFunction + run_real_optimizer.py and
are reported separately.

Usage:
    python scripts/validate_diversified_dataset.py [--save]
"""

import argparse
import hashlib
import json
import os
import statistics
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(REPO_ROOT))

# The aggregator lambda requires table names at import time; the
# dry run never touches AWS, so placeholder names are set
# (mirroring tests/test_aggregator.py's import pattern).
os.environ.setdefault("DOCUMENTS_TABLE", "dry-run-documents")
os.environ.setdefault("ACCESS_HISTORY_TABLE", "dry-run-access")
os.environ.setdefault("AGGREGATES_TABLE", "dry-run-aggregates")

from backend.lambdas.aggregates.app import (  # noqa: E402
    compute_aggregate_fields,
)
from generate_synthetic_data import (  # noqa: E402
    build_access_events,
    format_timestamp,
)
from optimization.adapters import from_aggregate_item  # noqa: E402
from optimization.constraints import (  # noqa: E402
    ALL_STORAGE_CLASSES,
    STATE_ELIGIBILITY,
    get_eligible_storage_classes,
)
from optimization.models import OptimizerInput  # noqa: E402
from optimization.access import calculate_access_frequency  # noqa: E402
from optimization.optimizer import optimize_document  # noqa: E402


DATASET_DIR = REPO_ROOT / "data" / "real" / "diversified"
MANIFEST_FILE = DATASET_DIR / "manifest.json"
WORKLOAD_FILE = DATASET_DIR / "workload_500.json"
PDF_DIR = DATASET_DIR / "pdfs"

RESULTS_JSON = (
    REPO_ROOT / "experiments" / "results"
    / "diversified_500_results.json"
)
VALIDATION_JSON = (
    REPO_ROOT / "experiments" / "results"
    / "diversified_500_validation.json"
)

REQUIRED_MANIFEST_FIELDS = (
    "document_id",
    "file_name",
    "source_url",
    "collection",
    "document_type",
    "date_issued",
    "file_size_bytes",
    "sha256",
    "content_type",
)


def sha256_file(path):
    digest = hashlib.sha256()

    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1 << 20), b""):
            digest.update(chunk)

    return digest.hexdigest()


def validate_corpus(report):
    """Checks 1-15 for the manifest/files."""

    problems = []

    raw = json.loads(
        MANIFEST_FILE.read_text(encoding="utf-8")
    )

    manifest = raw["documents"]

    report["total_documents"] = len(manifest)

    by_collection = Counter(m["collection"] for m in manifest)
    report["documents_by_collection"] = dict(by_collection)

    by_type = Counter(m["document_type"] for m in manifest)
    report["documents_by_document_type"] = dict(by_type)

    jurisdictions = Counter(
        m.get("jurisdiction", "") for m in manifest
    )
    report["documents_by_jurisdiction"] = dict(jurisdictions)

    courts = Counter(
        m.get("court", "")
        for m in manifest
        if m.get("court", "")
    )
    report["documents_by_court"] = dict(courts)

    agencies = Counter(
        m.get("agency", "")
        for m in manifest
        if m.get("agency", "")
    )
    report["documents_by_agency"] = dict(agencies)

    dates = sorted(m["date_issued"] for m in manifest)
    report["date_range"] = [dates[0], dates[-1]]

    total_bytes = 0
    sizes = []
    invalid_pdfs = 0
    missing_files = 0
    sha_mismatches = 0
    size_mismatches = 0

    for document in manifest:
        path = PDF_DIR / document["file_name"]

        if not path.exists():
            missing_files += 1
            problems.append(
                f"missing file: {document['file_name']}"
            )
            continue

        size = path.stat().st_size
        total_bytes += size
        sizes.append(size)

        if size != document["file_size_bytes"]:
            size_mismatches += 1

        with path.open("rb") as file:
            if file.read(4) != b"%PDF":
                invalid_pdfs += 1

        if sha256_file(path) != document["sha256"]:
            sha_mismatches += 1

    report["total_bytes"] = total_bytes
    report["size_min"] = min(sizes) if sizes else None
    report["size_median"] = (
        statistics.median(sizes) if sizes else None
    )
    report["size_mean"] = (
        statistics.mean(sizes) if sizes else None
    )
    report["size_max"] = max(sizes) if sizes else None
    report["invalid_pdfs_count"] = invalid_pdfs

    unique_ids = {m["document_id"] for m in manifest}
    report["duplicate_ids_count"] = len(manifest) - len(
        unique_ids
    )

    if len(unique_ids) != len(manifest):
        problems.append("duplicate document_ids")

    missing_metadata = 0

    for number, document in enumerate(manifest, 1):
        missing = [
            field
            for field in REQUIRED_MANIFEST_FIELDS
            if not document.get(field)
        ]

        if missing:
            missing_metadata += 1
            problems.append(
                f"manifest doc #{number}: missing "
                f"{missing}"
            )

    report["missing_metadata_records"] = missing_metadata

    report["sha256_mismatches"] = sha_mismatches
    report["size_mismatches"] = size_mismatches
    report["manifest_files_on_disk"] = {
        path.name for path in PDF_DIR.glob("*.pdf")
    }

    extra_on_disk = (
        report["manifest_files_on_disk"]
        - {m["file_name"] for m in manifest}
    )

    report["manifest_file_consistency"] = (
        "PASS"
        if not (
            extra_on_disk
            or missing_files
            or size_mismatches
            or sha_mismatches
        )
        else "FAIL"
    )
    report["files_on_disk_not_in_manifest"] = sorted(
        extra_on_disk
    )

    # Exactly 500 unique documents: count, id-uniqueness, and a
    # 1:1 manifest/file pairing.
    unique_ids = {m["document_id"] for m in manifest}

    report["exactly_500_unique_documents"] = bool(
        len(manifest) == 500
        and len(unique_ids) == 500
        and report["duplicate_ids_count"] == 0
        and report["manifest_file_consistency"] == "PASS"
    )

    if len(manifest) != 500:
        problems.append(
            f"document count {len(manifest)} != 500"
        )

    return problems, manifest


def validate_workload(manifest, report):
    """Workload distributions and feasibility."""

    raw = json.loads(
        WORKLOAD_FILE.read_text(encoding="utf-8")
    )

    workload = raw["documents"]

    by_id = {r["document_id"]: r for r in workload}

    manifest_ids = [m["document_id"] for m in manifest]

    if set(by_id) != set(manifest_ids):
        report["workload_manifest_match"] = False

        raise SystemExit(
            "workload document ids differ from manifest -- "
            "regenerate."
        )

    report["workload_manifest_match"] = True
    report["workload_seed"] = raw["random_seed"]

    total_events = sum(r["access_count"] for r in workload)
    report["total_workload_access_events"] = total_events

    band_counts = Counter(r["access_band"] for r in workload)
    report["access_band_distribution"] = dict(band_counts)

    by_order = ["zero", "low", "moderate", "high", "very high"]
    shares = {
        band: round(band_counts[band] / len(workload) * 100, 1)
        for band in by_order
    }
    report["access_band_percentages"] = shares

    # Histogram over actual counts.
    histogram = Counter()

    for r in workload:
        count = r["access_count"]

        if count == 0:
            histogram["0"] += 1
        elif count <= 5:
            histogram["1-5"] += 1
        elif count <= 30:
            histogram["6-30"] += 1
        elif count <= 100:
            histogram["31-100"] += 1
        else:
            histogram["101-300"] += 1

    report["access_count_histogram"] = dict(histogram)

    freqs = [r["access_frequency"] for r in workload]
    report["access_frequency_min"] = min(freqs)
    report["access_frequency_max"] = max(freqs)
    report["access_frequency_mean"] = (
        statistics.mean(freqs)
    )
    report["access_frequency_zero_count"] = sum(
        1 for f in freqs if f == 0.0
    )

    recencies = Counter(
        r["recency_band"] for r in workload
    )
    report["recency_band_distribution"] = dict(recencies)

    dates_since = [
        r["days_since_last_access"]
        for r in workload
        if r["days_since_last_access"] is not None
    ]

    if dates_since:
        report["days_since_last_access_min"] = min(dates_since)
        report["days_since_last_access_median"] = (
            statistics.median(dates_since)
        )
        report["days_since_last_access_max"] = max(dates_since)
    else:
        report["days_since_last_access_min"] = None
        report["days_since_last_access_median"] = None
        report["days_since_last_access_max"] = None

    classes = Counter(
        r["current_storage_class"] for r in workload
    )
    report["current_storage_class_distribution"] = dict(
        classes
    )

    states = Counter(
        str(r["document_state"]) for r in workload
    )
    report["document_state_distribution"] = dict(states)

    ages = Counter(r["age_band"] for r in workload)
    report["age_band_distribution"] = dict(ages)

    anchor = datetime.fromisoformat(
        raw["reference_timestamp"].replace("Z", "+00:00")
    )

    parsed_upload = [
        (
            anchor
            - datetime.fromisoformat(
                r["upload_timestamp"].replace("Z", "+00:00")
            )
        ).total_seconds()
        / 86400
        for r in workload
    ]
    report["document_age_min_days"] = round(
        min(parsed_upload), 1
    )
    report["document_age_median_days"] = round(
        statistics.median(parsed_upload), 1
    )
    report["document_age_max_days"] = round(
        max(parsed_upload), 1
    )

    eligible_distribution = Counter()

    for record in workload:
        eligible = get_eligible_storage_classes(
            record["document_state"]
        )
        eligible_distribution[
            "|".join(eligible)
        ] += 1

    report["eligible_storage_class_distribution"] = dict(
        eligible_distribution
    )

    # Eligibility violations in the CONTROLLED placements (these
    # are intentional, but must be counted, never hidden).
    violations = [
        record
        for record in workload
        if record["current_storage_class"]
        not in get_eligible_storage_classes(
            record["document_state"]
        )
    ]
    report["controlled_eligibility_violations"] = len(
        violations
    )

    return workload, by_id, report


def materialize_dry_run_inputs(
    workload_records, by_id, problems, report
):
    """
    Local dry-run: build DocumentsTable-shaped records, write
    events with the existing build_access_events, aggregate with
    the existing compute_aggregate_fields, then run the existing
    adapter + optimizer. Report the scenario coverage.
    """

    now = datetime.now(timezone.utc).replace(
        microsecond=0
    )

    aggregate_items = []
    optimizer_inputs = []
    schema_violations = []

    for record in workload_records:
        document_item = {
            "document_id": record["document_id"],
            "file_size": record["file_size_bytes"],
            "current_storage_class": record[
                "current_storage_class"
            ],
            "upload_timestamp": format_timestamp(
                datetime.fromisoformat(
                    record["upload_timestamp"].replace(
                        "Z", "+00:00"
                    )
                )
            ),
        }

        if record["document_state"] is not None:
            document_item["document_state"] = record[
                "document_state"
            ]

        events = build_access_events(
            {
                "document_id": record["document_id"],
                "access_count": record["access_count"],
                "days_since_last_access": record[
                    "days_since_last_access"
                ]
                or 0.0,
            },
            now,
        )

        aggregate_item = compute_aggregate_fields(
            document_item, events, now
        )

        aggregate_items.append(aggregate_item)

        # Existing adapter -> OptimizerInput schema check.
        try:
            optimizer_input = from_aggregate_item(
                aggregate_item
            )
        except Exception as error:  # noqa: BLE001
            schema_violations.append(
                f"{record['document_id']}: adapter failed "
                f"({error})"
            )
            continue

        try:
            _check_schema(
                optimizer_input, record["document_id"]
            )
        except AssertionError as error:
            schema_violations.append(str(error))

        optimizer_inputs.append(optimizer_input)

    if schema_violations:
        problems.extend(schema_violations)
        report["optimizer_input_schema"] = "FAIL"
    else:
        report["optimizer_input_schema"] = f"PASS ({len(optimizer_inputs)} inputs)"  # noqa: E501

    # Dry optimizer run (existing optimize_document only).
    verdicts = Counter()
    recommended = Counter()
    current_recommended_matrix = Counter()
    current_cost_total = 0.0
    recommended_cost_total = 0.0
    optimizer_errors = []

    for optimizer_input in optimizer_inputs:
        try:
            result = optimize_document(optimizer_input)
        except Exception as error:  # noqa: BLE001
            optimizer_errors.append(
                f"{optimizer_input.document_id}: "
                f"{type(error).__name__}: {error}"
            )
            continue

        verdicts[_classify(result.savings)] += 1
        recommended[
            result.recommended_storage_class
        ] += 1
        current_recommended_matrix[
            f"{optimizer_input.current_storage_class}"
            f" -> {result.recommended_storage_class}"
        ] += 1

        current_cost_total += result.current_cost
        recommended_cost_total += result.recommended_cost

    savings = current_cost_total - recommended_cost_total
    savings_percentage = (
        (savings / current_cost_total) * 100
        if current_cost_total > 0
        else 0.0
    )

    dry = {
        "optimizer_errors": optimizer_errors,
        "verdict_counts": dict(verdicts),
        "recommended_distribution": dict(recommended),
        "current_to_recommended_counts": dict(
            current_recommended_matrix
        ),
        "policy_a_current_projected_annual_cost": round(
            current_cost_total, 10
        ),
        "policy_b_recommended_projected_annual_cost": round(
            recommended_cost_total, 10
        ),
        "projected_savings": round(savings, 10),
        "savings_percentage": round(savings_percentage, 4),
    }

    report["dry_run"] = dry

    return aggregate_items, optimizer_inputs, dry


def _classify(savings):
    if abs(savings) <= 1e-6:
        return "NO CHANGE"

    if savings > 0:
        return "POSITIVE SAVINGS"

    return "NEGATIVE SAVINGS"


def _check_schema(input_value, document_id):
    """Assert every field of the OptimizerInput contract."""

    assert isinstance(input_value.document_id, str) and (
        input_value.document_id
    ), document_id

    assert isinstance(input_value.file_size_bytes, int), (
        document_id
    )
    assert 0 < input_value.file_size_bytes, document_id

    classes = set(ALL_STORAGE_CLASSES)
    assert (
        input_value.current_storage_class in classes
    ), document_id

    datetime.fromisoformat(
        input_value.upload_timestamp.replace("Z", "+00:00")
    )

    if input_value.last_accessed_timestamp is not None:
        datetime.fromisoformat(
            input_value.last_accessed_timestamp.replace(
                "Z", "+00:00"
            )
        )

    assert isinstance(input_value.access_count, int), (
        document_id
    )
    assert input_value.access_count >= 0, document_id

    if input_value.days_since_last_access is not None:
        value = input_value.days_since_last_access
        assert 0 <= value < 30, document_id

    expected_frequency = calculate_access_frequency(
        input_value.access_count
    )
    assert abs(
        input_value.access_frequency - expected_frequency
    ) < 1e-9, document_id

    if input_value.document_state is not None:
        assert (
            input_value.document_state in STATE_ELIGIBILITY
        ), document_id


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Full validation report for the diversified "
            "corpus and workload."
        )
    )
    parser.add_argument(
        "--save",
        action="store_true",
        help="Write the JSON report to experiments/results/.",
    )
    args = parser.parse_args()

    report = {}
    problems = []

    corpus_problems, manifest = validate_corpus(report)
    problems.extend(corpus_problems)

    workload_records, by_id, workload_report = (
        validate_workload(manifest, report)
    )

    materialize_dry_run_inputs(
        workload_records, by_id, problems, report
    )

    report["validation_problems"] = problems
    report["validation_result"] = (
        "PASS" if not problems else "FAIL"
    )

    print("=" * 64)
    print("DIVERSIFIED DATASET VALIDATION REPORT")
    print("=" * 64)

    for key, value in report.items():
        if key == "dry_run":
            continue

        if isinstance(value, dict):
            print(f"{key}:")
            for inner_key, inner_value in value.items():
                print(f"    {inner_key}: {inner_value}")
        else:
            print(f"{key}: {value}")

    print("-" * 64)
    print("DRY-RUN DECISION COVERAGE (existing optimizer):")

    dry = report["dry_run"]

    for key in (
        "verdict_counts",
        "recommended_distribution",
        "eligible_storage_class_distribution",
        "policy_a_current_projected_annual_cost",
        "policy_b_recommended_projected_annual_cost",
        "projected_savings",
        "savings_percentage",
        "optimizer_errors",
    ):
        print(f"  {key}: {dry.get(key)}")

    transitions = dry["current_to_recommended_counts"]

    print(
        f"  distinct current->recommended combinations: "
        f"{len(transitions)}"
    )
    print("-" * 64)
    print(
        f"VALIDATION: {report['validation_result']}"
        + (
            ""
            if not problems
            else f" ({len(problems)} problems)"
        )
    )

    for problem in problems:
        print(f"  PROBLEM: {problem}")

    if args.save:
        VALIDATION_JSON.write_text(
            json.dumps(report, indent=2, default=str),
            encoding="utf-8",
        )
        print(f"Report saved: {VALIDATION_JSON}")

    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())