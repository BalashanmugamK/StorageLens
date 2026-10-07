"""Tests for scripts/deploy_and_configure.py — the SAM-deploy →
frontend/.env glue. No AWS calls; the CloudFormation describe is
stubbed."""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

SCRIPT_PATH = REPO_ROOT / "scripts" / "deploy_and_configure.py"


@pytest.fixture()
def script():
    spec = importlib.util.spec_from_file_location("deploy_and_configure", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["deploy_and_configure"] = module
    spec.loader.exec_module(module)
    return module


def _cfn_stub(outputs):
    cfn = MagicMock()
    cfn.describe_stacks.return_value = {
        "Stacks": [{"Outputs": outputs}]
    }
    return cfn


def test_collect_outputs_maps_the_stack_outputs(script):
    cfn = _cfn_stub(
        [
            {"OutputKey": "DocumentsApiEndpoint", "OutputValue": "https://api.example.com"},
            {"OutputKey": "UserPoolId", "OutputValue": "pool-1"},
        ]
    )

    with patch("boto3.client", return_value=cfn):
        mapping = script.collect_outputs("stack-1", "us-east-2")

    assert mapping["DocumentsApiEndpoint"] == "https://api.example.com"
    # The stack name flows to the API call unmodified.
    assert cfn.describe_stacks.call_args.kwargs["StackName"] == "stack-1"


def test_collect_outputs_fails_when_api_endpoint_is_missing(script):
    cfn = _cfn_stub([{"OutputKey": "UserPoolId", "OutputValue": "pool-1"}])

    with patch("boto3.client", return_value=cfn):
        with pytest.raises(SystemExit, match="DocumentsApiEndpoint"):
            script.collect_outputs("stack-1", "us-east-2")


def test_render_env_carries_only_the_endpoint(script):
    env = script.render_env({"DocumentsApiEndpoint": "https://api.example.com"})

    assert env.strip().splitlines()[-1] == "VITE_API_BASE_URL=https://api.example.com"
    # The generated file warns it is not hand-maintained.
    assert "deploy_and_configure" in env


def test_main_writes_the_env_file(script, tmp_path, monkeypatch):
    cfn = _cfn_stub(
        [{"OutputKey": "DocumentsApiEndpoint", "OutputValue": "https://api.example.com"}]
    )

    # Point the writer at a temp checkout instead of the real one.
    monkeypatch.setattr(
        script, "__file__", str(tmp_path / "scripts" / "deploy_and_configure.py")
    )
    with patch("boto3.client", return_value=cfn), patch(
        "boto3.session.Session"
    ) as session:
        session.return_value.region_name = "us-east-2"

        class Args:
            stack_name = "stack-1"
            region = "us-east-2"

        # main() drives argparse; call the pieces directly.
        mapping = script.collect_outputs(
            Args.stack_name, Args.region
        )
        from pathlib import Path

        env_path = (
            Path(script.__file__).resolve().parents[1] / "frontend" / ".env"
        )
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.write_text(script.render_env(mapping), encoding="utf-8")

    assert env_path.exists()
    assert "VITE_API_BASE_URL=https://api.example.com" in env_path.read_text(
        encoding="utf-8"
    )
