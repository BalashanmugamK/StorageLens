"""Tests for scripts/deploy_frontend.py — the build → bucket → cache
glue. No AWS calls and no npm runs; S3/CloudFormation/CloudFront are
stubbed, the build is a recorded `npm run build`."""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

SCRIPT_PATH = REPO_ROOT / "scripts" / "deploy_frontend.py"


@pytest.fixture()
def script():
    spec = importlib.util.spec_from_file_location("deploy_frontend", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["deploy_frontend"] = module
    spec.loader.exec_module(module)
    return module


def _cfn_stub(outputs):
    cfn = MagicMock()
    cfn.describe_stacks.return_value = {"Stacks": [{"Outputs": outputs}]}
    return cfn


def test_collect_outputs_fails_when_bucket_output_is_missing(script):
    cfn = _cfn_stub([{"OutputKey": "DocumentsApiEndpoint", "OutputValue": "x"}])
    with patch("boto3.client", return_value=cfn):
        with pytest.raises(SystemExit, match="FrontendBucketName"):
            script.collect_outputs("stack-1", "ap-south-1")


def test_content_types_cover_every_dist_extension(script):
    # A Vite build of this app emits (at least) these five kinds of
    # file; each must get a browser-resolvable content type.
    for key in ("index.html", "assets/index-a1.js", "assets/index-b2.css",
                "favicon.svg", "public/artifacts.json"):
        content_type, cache = script.content_type_and_cache(key)
        assert content_type != "application/octet-stream", key
    # HTML (the SPA shell) must revalidate so a new deploy is seen,
    # while hashed assets are immutable.
    _, html_cache = script.content_type_and_cache("index.html")
    _, asset_cache = script.content_type_and_cache("assets/index-a1.js")
    assert html_cache == "no-cache"
    assert "immutable" in asset_cache


def test_collect_upload_files_requires_a_built_dist(script, tmp_path):
    with pytest.raises(SystemExit, match="index.html"):
        script.collect_upload_files(tmp_path)

    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<html>", encoding="utf-8")
    (tmp_path / "assets" / "index-a1.js").write_text("0", encoding="utf-8")

    files = script.collect_upload_files(tmp_path)
    assert files == [
        (tmp_path / "assets" / "index-a1.js", "assets/index-a1.js"),
        (tmp_path / "index.html", "index.html"),
    ]


def test_upload_dist_overwrites_and_prunes_old_deploy(script, tmp_path):
    # New deploy: index.html + one hashed asset. The bucket still has
    # last time's stale asset, which must be deleted.
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<html>", encoding="utf-8")
    (tmp_path / "assets" / "index-a1.js").write_text("0", encoding="utf-8")

    s3 = MagicMock()
    pages = [{ "Contents": [
        {"Key": "index.html"},
        {"Key": "assets/index-stale.js"},
    ]}]
    paginator = MagicMock(); paginator.paginate.return_value = pages
    s3.get_paginator.return_value = paginator

    uploaded, pruned = script.upload_dist(tmp_path, "frontend-bucket", s3)

    assert uploaded == ["assets/index-a1.js", "index.html"]
    assert pruned == ["assets/index-stale.js"]
    stale = [c for c in s3.delete_object.call_args_list]
    assert any(c.kwargs["Key"] == "assets/index-stale.js" for c in stale)
    # Immutable content lives under assets/, HTML revalidates.
    puts = {c.kwargs["Key"]: c.kwargs for c in s3.put_object.call_args_list}
    assert puts["index.html"]["CacheControl"] == "no-cache"
    assert "immutable" in puts["assets/index-a1.js"]["CacheControl"]
    assert puts["index.html"]["ContentType"].startswith("text/html")


def test_collect_outputs_tolerates_a_restored_stack_without_distribution(script):
    # The restore excluded FrontendDistribution/...Policy while the
    # account was pending CloudFront verification; its outputs are
    # absent but the script must still collect what exists.
    cfn = _cfn_stub([
        {"OutputKey": "FrontendBucketName", "OutputValue": "b-1"},
        {"OutputKey": "DocumentsApiEndpoint", "OutputValue": "x"},
    ])
    with patch("boto3.client", return_value=cfn):
        mapping = script.collect_outputs("stack-1", "ap-south-1")

    assert mapping.get(script.DISTRIBUTION_OUTPUT) is None


def test_find_distribution_returns_the_stack_resource_id(script):
    cfn = _cfn_stub([])
    cfn.list_stack_resources.return_value = {
        "StackResourceSummaries": [
            {"LogicalResourceId": "FrontendBucket", "PhysicalResourceId": "b-1"},
            {"LogicalResourceId": "FrontendDistribution",
             "PhysicalResourceId": "E1234567890"},
        ]
    }
    with patch("boto3.client", return_value=cfn):
        assert script.find_distribution("stack-1", "ap-south-1") == "E1234567890"

    cfn.list_stack_resources.return_value = {"StackResourceSummaries": []}
    with patch("boto3.client", return_value=cfn):
        assert script.find_distribution("stack-1", "ap-south-1") is None


def test_invalidate_cache_targets_the_given_distribution(script):
    cf = MagicMock()
    cf.create_invalidation.return_value = {"Invalidation": {"Id": "inv-1"}}

    script.invalidate_cache("E1234567890", cloudfront=cf)

    cf.create_invalidation.assert_called_once()
    kwargs = cf.create_invalidation.call_args.kwargs
    assert kwargs["DistributionId"] == "E1234567890"
    assert kwargs["InvalidationBatch"]["Paths"]["Items"] == ["/*"]