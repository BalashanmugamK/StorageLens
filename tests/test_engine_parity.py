"""Byte-identity pin between the deployed decisions Lambda's engine
bundle and the optimization/ package it copies.

backend/lambdas/decisions/engine/ exists because AWS Lambda deploys
a single directory, and the write half of the loop must run the EXACT
cost model the offline experiments ran - a hand-synced fork is the
same trap the frontend estimator avoids with its bundle-meta pin.

If either file changes without the other, this suite fails instead
of live recommendations silently drifting from the experiments.
"""

import filecmp
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

SOURCE = REPO_ROOT / "optimization"

# Two lambdas ship the engine: the decisions lambda (recommendations
# and the ledger) and the reconciliation lambda (model vs bill). Both
# copies must stay byte-identical, or the shipped ledger and the
# reconciliation that judges it would price with different models.
COPIES = [
    REPO_ROOT / "backend" / "lambdas" / "decisions" / "engine",
    REPO_ROOT / "backend" / "lambdas" / "reconciliation" / "engine",
]

# Every module the lambdas import, plus the pricing snapshot the
# cost model loads at import time. optimizer/costs pull in access,
# models and constraints transitively.
ENGINE_FILES = [
    "__init__.py",
    "access.py",
    "adapters.py",
    "confidence.py",
    "constraints.py",
    "costs.py",
    "models.py",
    "optimizer.py",
    "pricing.json",
]


def test_engine_bundle_is_byte_identical():
    for copy in COPIES:
        for name in ENGINE_FILES:
            source = SOURCE / name
            copied = copy / name

            assert source.exists(), f"missing engine source {name}"
            assert copied.exists(), (
                f"missing engine copy {name} in {copy}"
            )

            assert filecmp.cmp(
                source, copied, shallow=False
            ), (
                f"{name} diverged between optimization/ and "
                f"{copy} - copy it again or change both"
            )


def test_bundle_imports_resolve_offline():
    """The copied package must import and run its optimizer without
    the optimization/ package present - i.e. genuinely standalone,
    as it will be inside the Lambda. Runs in an isolated interpreter
    (python -I: no cwd auto-import, no PYTHONPATH, no test-suite
    module cache) with ONLY the lambda directory on the path."""

    import os
    import subprocess
    import sys

    lambda_path = str(REPO_ROOT / "backend" / "lambdas").replace(
        "\\", "/"
    )

    # The engine input mirrors the cold fixture in
    # test_decision_lambda.py; expected output from the pinned
    # engine tests.
    script = (
        "import sys; sys.path.insert(0, '{lambda_dir}');"
        "from decisions.engine.optimizer import optimize_document;"
        "from decisions.engine.models import OptimizerInput;"
        "input_model = OptimizerInput("
        "document_id='parity', file_size_bytes=10_000_000_000,"
        "current_storage_class='STANDARD',"
        "upload_timestamp='2026-01-01T00:00:00Z',"
        "last_accessed_timestamp='2026-02-01T00:00:00Z',"
        "access_count=0, days_since_last_access=240.0,"
        "access_frequency=0.0,"
        "aggregation_timestamp='2026-04-01T00:00:00Z',"
        "document_state='ARCHIVED');"
        "result = optimize_document(input_model, 'B');"
        "assert result.recommended_storage_class == 'GLACIER_DEEP_ARCHIVE';"
        "assert result.savings > 0;"
        "print('PARITY OK')"
    ).replace("{lambda_dir}", lambda_path)

    result = subprocess.run(
        [sys.executable, "-I", "-c", script],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        timeout=60,
    )

    assert result.returncode == 0, (
        f"standalone engine import failed:\n{result.stderr}"
    )
    assert "PARITY OK" in result.stdout

    # And the optimization/ package must NOT have crept onto the
    # interpreter's path in that run.
    assert "optimization" not in result.stdout


def test_pricing_snapshot_not_stale_in_bundle():
    """The pricing freshness warning applies to the deployed engine
    copy too - it shares a byte-identical snapshot (see the file test
    above); this pins the repo copy's date for the deployment gate."""

    import json

    pricing = json.loads(
        (COPIES[0] / "pricing.json").read_text(encoding="utf-8")
    )

    assert pricing["pricing_checked"]
    assert pricing["region"]
    assert pricing["currency"]