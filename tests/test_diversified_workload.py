"""
Tests for the diversified controlled workload generator: schema,
band coverage, feasibility constraints, schema-conformity of the
resulting optimizer inputs, and seed determinism.
"""

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import generate_diversified_workload as gw  # noqa: E402
from optimization.constraints import (  # noqa: E402
    ALL_STORAGE_CLASSES,
    get_eligible_storage_classes,
)
from optimization.adapters import from_aggregate_item  # noqa: E402
from optimization.models import OptimizerInput  # noqa: E402


SAMPLE_MANIFEST = {
    "documents": [
        {
            "document_id": f"USCOURTS-ilnd-{number}",
            "package_id": f"USCOURTS-ilnd-pkg-{number}",
            "granule_id": f"USCOURTS-ilnd-{number}",
            "file_name": f"USCOURTS-ilnd-{number}.pdf",
            "source_url": "https://www.govinfo.gov/x.pdf",
            "collection": "USCOURTS",
            "document_type": "district_court_opinion",
            "jurisdiction": "United States",
            "court": "United States District Court "
            "Northern District of Illinois",
            "agency": "",
            "collection_label": "test",
            "title": "Test v. Case",
            "date_issued": "2011-06-15",
            "file_size_bytes": 700_000,
            "sha256": "0" * 64,
            "content_type": "application/pdf",
        }
        for number in range(40)
    ]
}


@pytest.fixture()
def sample_manifest_file(tmp_path):
    path = tmp_path / "manifest.json"

    path.write_text(
        json.dumps(SAMPLE_MANIFEST), encoding="utf-8"
    )

    return path


def test_workload_generation_deterministic(sample_manifest_file, tmp_path):
    out_a = tmp_path / "a.json"
    out_b = tmp_path / "b.json"

    gw.generate_workload(sample_manifest_file, out_a)
    gw.generate_workload(sample_manifest_file, out_b)

    assert out_a.read_bytes() == out_b.read_bytes()


def test_workload_schema_and_feasibility(sample_manifest_file, tmp_path):
    out = tmp_path / "workload.json"

    gw.generate_workload(sample_manifest_file, out)

    records = json.loads(out.read_text(encoding="utf-8"))["documents"]

    anchor = gw.datetime.fromisoformat(
        gw.WORKLOAD_ANCHOR.replace("Z", "+00:00")
    )

    assert len(records) == len(SAMPLE_MANIFEST["documents"])

    for record in records:
        # Complete schema.
        for field in (
            "document_id",
            "current_storage_class",
            "document_state",
            "upload_timestamp",
            "access_count",
            "access_frequency",
            "days_since_last_access",
            "access_band",
            "recency_band",
            "age_band",
        ):
            assert field in record, (field, record)

        # Frequency always derives from the count (existing
        # implementation, never invented independently).
        assert record["access_frequency"] == record[
            "access_count"
        ] / 30.0

        assert record["current_storage_class"] in (
            ALL_STORAGE_CLASSES
        )

        if record["document_state"] is not None:
            assert record["document_state"] in (
                "ACTIVE",
                "CLOSED",
                "ARCHIVED",
            )

        # Recency stays inside the 30-day observation window.
        if record["days_since_last_access"] is not None:
            assert 0 <= record["days_since_last_access"] < 30
        else:
            assert record["access_count"] == 0
            assert record["last_accessed_timestamp"] is None

        # Upload timestamp: real issue date and the event span.
        upload = gw.datetime.fromisoformat(
            record["upload_timestamp"].replace("Z", "+00:00")
        )

        date_issued = gw.datetime.fromisoformat("2011-06-15").replace(
            tzinfo=gw.timezone.utc
        )

        assert upload >= date_issued
        assert upload < anchor

        age_days = (
            anchor - upload
        ).total_seconds() / 86400.0

        if record["access_count"] > 0:
            # Every materialized event post-dates the upload:
            # span room = SPAN_FRACTION * (29 - days_since).
            span_room = (
                29.0 - record["days_since_last_access"]
            ) * gw.SPAN_FRACTION

            assert age_days >= (
                record["days_since_last_access"] + span_room
            ), record

        assert 0 <= age_days <= 1200


def test_workload_band_coverage_matches_shares(sample_manifest_file, tmp_path):
    out = tmp_path / "workload.json"

    gw.generate_workload(sample_manifest_file, out)

    records = json.loads(out.read_text(encoding="utf-8"))["documents"]

    sample_size = len(SAMPLE_MANIFEST["documents"])

    for field, expected_bands in (
        (
            "access_band",
            [
                ("zero", 0.20),
                ("low", 0.30),
                ("moderate", 0.30),
                ("high", 0.15),
                ("very high", 0.05),
            ],
        ),
        (
            "age_band",
            [("new", 0.15), ("medium", 0.35), ("old", 0.50)],
        ),
        (
            "recency_band",
            [
                ("none", 0.20),
                ("very recent", 0.24),
                ("recent", 0.28),
                ("moderately stale", 0.16),
                ("stale", 0.12),
            ],
        ),
        (
            "document_state",
            [("ACTIVE", 0.50), ("CLOSED", 0.25), ("ARCHIVED", 0.15)],
        ),
        (
            "current_storage_class",
            [
                ("STANDARD", 0.30),
                ("STANDARD_IA", 0.20),
                (
                    "GLACIER_INSTANT_RETRIEVAL",
                    0.15,
                ),
                (
                    "GLACIER_FLEXIBLE_RETRIEVAL",
                    0.20,
                ),
                ("GLACIER_DEEP_ARCHIVE", 0.10),
            ],
        ),
    ):
        present = {r[field] for r in records}

        # A band must appear when its expected sample count is
        # large enough to make absence vanishingly unlikely.
        for band, share in expected_bands:
            if sample_size * share >= 5:
                assert band in present, (field, band)


def test_generated_optimizer_inputs_satisfy_schema(sample_manifest_file, tmp_path):
    """
    Every generated record must survive the existing adapter into
    the OptimizerInput schema (state upper-handling, types, etc.).
    """

    out = tmp_path / "workload.json"

    gw.generate_workload(sample_manifest_file, out)

    records = json.loads(out.read_text(encoding="utf-8"))["documents"]

    for record in records:
        aggregate_item = {
            "document_id": record["document_id"],
            "file_size_bytes": record["file_size_bytes"],
            "current_storage_class": record[
                "current_storage_class"
            ],
            "upload_timestamp": record["upload_timestamp"],
            "last_accessed_timestamp": record[
                "last_accessed_timestamp"
            ],
            "access_count": record["access_count"],
            "days_since_last_access": record[
                "days_since_last_access"
            ],
            "access_frequency": record["access_frequency"],
            "aggregation_timestamp": gw.WORKLOAD_ANCHOR,
            "document_state": record["document_state"],
        }

        optimizer_input = from_aggregate_item(aggregate_item)

        assert isinstance(optimizer_input, OptimizerInput)

        assert optimizer_input.access_count == record[
            "access_count"
        ]


def test_violating_placements_are_detected():
    """
    The controlled workload places a small share of documents in
    classes their state forbids; the existing eligibility rules
    must flag those (they are the optimizer's target scenario).
    """

    placements = gw.CURRENT_CLASS_SHARES

    violations = []

    for state, classes in placements.items():
        eligible = set(get_eligible_storage_classes(state))

        for storage_class, _share in classes:
            if storage_class not in eligible:
                violations.append((state, storage_class))

    # Violations exist in some states (that is the design)...
    assert violations

    # ...but never dominate: the shares stay realistic.
    for state, classes in placements.items():
        eligible_share = sum(
            share
            for storage_class, share in classes
            if storage_class
            in set(get_eligible_storage_classes(state))
        )

        assert eligible_share >= 0.60, (state, eligible_share)


def test_project_workload_file_is_valid_when_present():
    workload_file = REPO_ROOT / "data" / "real" / "diversified" / (
        "workload_500.json"
    )

    if not workload_file.exists():
        pytest.skip("diversified workload not generated yet")

    raw = json.loads(workload_file.read_text(encoding="utf-8"))

    assert raw["document_count"] == 500
    assert len(raw["documents"]) == 500

    documents = raw["documents"]

    assert len({d["document_id"] for d in documents}) == 500

    assert all(
        d["access_frequency"] == d["access_count"] / 30.0
        for d in documents
    )

    band_counts = {}
    for d in documents:
        band_counts[d["access_band"]] = (
            band_counts.get(d["access_band"], 0) + 1
        )

    shares = {
        band: round(
            band_counts.get(band, 0) / 500 * 100, 1
        )
        for band in (
            "zero",
            "low",
            "moderate",
            "high",
            "very high",
        )
    }

    # Target distribution tolerance: 2 percentage points.
    for band, target in (
        ("zero", 20.0),
        ("low", 30.0),
        ("moderate", 30.0),
        ("high", 15.0),
        ("very high", 5.0),
    ):
        assert abs(shares[band] - target) <= 2.0, (
            band,
            shares[band],
        )