"""
Tests for dataset-agnostic loading: the loader's manifest
schema validation and per-dataset path resolution must work for
any public legal dataset, not just the us_courts corpus.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from load_real_data import resolve_dataset_paths, validate_manifest_schema


def test_validate_manifest_schema_accepts_generic_dataset():
    documents = [
        {
            "document_id": "cfr-2024-title-47-part-1",
            "file_name": "cfr-2024-title-47-part-1.pdf",
            "file_size_bytes": 1_234_567,
            "content_type": "application/pdf",
        }
    ]

    validate_manifest_schema(documents)


def test_validate_manifest_schema_flags_missing_fields():
    documents = [
        {
            "document_id": "doc-1",
            "file_name": "doc-1.pdf",
            "file_size_bytes": 100,
            # content_type deliberately omitted
        },
        {
            "document_id": "doc-2",
            # file_name and content_type omitted
            "file_size_bytes": 200,
        },
    ]

    with pytest.raises(RuntimeError) as excinfo:
        validate_manifest_schema(documents)

    message = str(excinfo.value)
    assert "content_type" in message
    assert "file_name" in message


def test_validate_manifest_schema_flags_bad_sizes():
    documents = [
        {
            "document_id": "doc-1",
            "file_name": "doc-1.pdf",
            "file_size_bytes": 0,
            "content_type": "application/pdf",
        },
        {
            "document_id": "doc-2",
            "file_name": "doc-2.pdf",
            "file_size_bytes": 1024,
            "content_type": "application/pdf",
        },
    ]

    with pytest.raises(RuntimeError) as excinfo:
        validate_manifest_schema(documents)

    assert "file_size_bytes" in str(excinfo.value)


def test_resolve_dataset_paths_defaults_preserve_phase_b():
    paths = resolve_dataset_paths(
        "us_courts", "workload_500.json", None
    )

    assert paths["manifest"].name == "manifest.json"
    assert paths["pdf_dir"].name == "pdfs"
    assert paths["workload"].name == "workload_500.json"
    assert paths["mapping"].name == "real_500_mapping.json"


def test_resolve_dataset_paths_other_dataset_gets_own_files():
    paths = resolve_dataset_paths(
        "cfr_2024", None, None
    )

    assert paths["mapping"].name == "real_cfr_2024_mapping.json"

    # A dataset can also pin its own mapping file explicitly.
    pinned = resolve_dataset_paths(
        "cfr_2024", None, "my_dataset_mapping.json"
    )


    assert pinned["mapping"].name == "my_dataset_mapping.json"

    assert pinned["manifest"].name == "manifest.json"