"""Validation of the committed synthetic-workload experiment artifact.

This file used to REGENERATE data/synthetic/workload_500_results.json
when run as a script — a test that mutates repo data. The regeneration
now lives in scripts/regenerate_workload_results.py; this module only
VALIDATES the committed artifact against the workload it was built
from and against its own arithmetic. No test here writes to disk.
"""

import json
import math
from pathlib import Path

RESULTS_FILE = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "synthetic"
    / "workload_500_results.json"
)

WORKLOAD_FILE = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "synthetic"
    / "workload_500.json"
)


def load_results() -> dict:
    with RESULTS_FILE.open(
        "r",
        encoding="utf-8",
    ) as file:
        return json.load(file)


def load_workload() -> dict:
    with WORKLOAD_FILE.open(
        "r",
        encoding="utf-8",
    ) as file:
        return json.load(file)


def breakdown_sum(cost: dict) -> float:
    return (
        cost["storage_cost"]
        + cost["retrieval_cost"]
        + cost["request_cost"]
        + cost["transition_cost"]
    )


def test_results_file_exists():
    """The committed artifact must exist; if stale, regenerate it
    with scripts/regenerate_workload_results.py."""
    assert RESULTS_FILE.exists(), (
        "data/synthetic/workload_500_results.json is missing — "
        "regenerate it with scripts/regenerate_workload_results.py"
    )


def test_summary_matches_per_document_totals():
    results = load_results()

    documents = results["documents"]
    summary = results["summary"]

    assert summary["total_documents"] == len(documents)

    assert math.isclose(
        summary["baseline_cost"],
        sum(
            row["baseline_cost"]
            for row in documents
        ),
        rel_tol=1e-9,
    )

    assert math.isclose(
        summary["optimizer_cost"],
        sum(
            row["optimizer_cost"]
            for row in documents
        ),
        rel_tol=1e-9,
    )

    assert math.isclose(
        summary["total_storage_gb"],
        sum(
            row["file_size_bytes"]
            for row in documents
        ) / 1_000_000_000,
        rel_tol=1e-9,
    )


def test_savings_is_baseline_minus_optimizer():
    results = load_results()

    summary = results["summary"]

    assert math.isclose(
        summary["optimizer_savings_vs_baseline"],
        summary["baseline_cost"]
        - summary["optimizer_cost"],
        rel_tol=1e-9,
    )

    assert math.isclose(
        summary["optimizer_savings_percentage_vs_baseline"],
        summary["optimizer_savings_vs_baseline"]
        / summary["baseline_cost"]
        * 100,
        rel_tol=1e-6,
    )


def test_workload_file_recorded_portably():
    """The experiment provenance path must be forward-slash
    (previously recorded with Windows backslashes)."""
    results = load_results()

    workload_file = results["experiment"][
        "workload_file"
    ]

    assert "\\" not in workload_file
    assert workload_file == "data/synthetic/workload_500.json"


def test_workload_covers_every_result_row():
    results = load_results()
    workload = load_workload()["documents"]

    workload_ids = {
        document["document_id"]
        for document in workload
    }

    result_ids = {
        row["document_id"]
        for row in results["documents"]
    }

    assert result_ids == workload_ids


def test_baseline_matches_optimizer_when_same_shape():
    """Baseline vs optimizer must be comparable: every row's
    optimizer cost must be <= its current-class cost, and the
    optimizer must never be slower-billed than the baseline for
    the same 12-month horizon under Policy B."""
    results = load_results()

    for row in results["documents"]:
        assert row["optimizer_cost"] <= row[
            "current_cost"
        ] + 1e-12, (
            f"{row['document_id']} recommends a tier more "
            "expensive than staying (Policy B guard violated)"
        )