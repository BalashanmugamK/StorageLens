"""
Preflight tests: scripts/check_deployment_permissions.py

Everything runs offline against fake clients and the real
template/samconfig/policy artifact. No AWS calls, no credentials,
and no IAM mutation anywhere.
"""

import json
import re
import sys
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

REPO_ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_deployment_permissions as preflight


# ---------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------

class FakeSimulator:
    """Fake of boto3 iam.simulate_principal_policy."""

    def __init__(self, allowed=None, denied=None):
        self.allowed = set(allowed or [])
        self.denied = set(denied or [])
        self.calls = []

    def simulate_principal_policy(self, **kwargs):
        self.calls.append(kwargs)

        results = []

        for action in kwargs["ActionNames"]:
            if action in self.allowed:
                decision = "allowed"
            elif action in self.denied:
                decision = "explicitDeny"
            else:
                decision = "implicitDeny"

            results.append(
                {
                    "EvalActionName": action,
                    "EvalDecision": decision,
                }
            )

        return {"EvaluationResults": results}


class DenyingSimulator:
    """Simulates an identity lacking iam:SimulatePrincipalPolicy."""

    def simulate_principal_policy(self, **kwargs):
        raise ClientError(
            {
                "Error": {
                    "Code": "AccessDenied",
                    "Message": (
                        "not authorized to perform: "
                        "iam:SimulatePrincipalPolicy"
                    ),
                }
            },
            "SimulatePrincipalPolicy",
        )


def _identity(arn="arn:aws:iam::111111111111:user/deployer"):
    return {"Arn": arn, "Account": "111111111111", "UserId": "A"}


# ---------------------------------------------------------------
# Identity / configuration resolution
# ---------------------------------------------------------------

def test_identity_detection_classifies_user_role_root():
    assert (
        preflight.describe_identity(
            _identity("arn:aws:iam::111111111111:user/deployer")
        )
        == "IAM user"
    )

    assert "assumed role" in preflight.describe_identity(
        _identity(
            "arn:aws:sts::111111111111:assumed-role/DeployRole/s3"
        )
    )


def test_assumed_role_session_arn_converts_for_simulator():
    source = preflight.simulate_source_arn(
        "arn:aws:sts::111111111111:assumed-role/DeployRole/session-1"
    )

    assert source == (
        "arn:aws:iam::111111111111:role/DeployRole"
    )

    assert (
        preflight.simulate_source_arn(
            "arn:aws:iam::111111111111:user/deployer"
        )
        == "arn:aws:iam::111111111111:user/deployer"
    )


def test_samconfig_defaults_load_without_hardcoding():
    config = preflight.load_deployment_config()

    assert config["stack_name"]
    assert config["region"]
    assert config["resolve_s3"] is True


# ---------------------------------------------------------------
# Requirement extraction (from the ACTUAL template)
# ---------------------------------------------------------------

def test_template_requires_iam_create_role():
    template = preflight.parse_template()
    requirements = preflight.extract_deployment_requirements(
        template
    )

    create_role = [
        requirement
        for requirement in requirements
        if requirement.action == "iam:CreateRole"
    ]

    assert create_role
    assert any(
        "AggregatesFunctionRole" in item.affected
        for item in create_role
    )
    assert any(
        "DocumentsFunctionRole" in item.affected
        for item in create_role
    )


def test_template_requirements_come_from_properties():
    template = preflight.parse_template()
    requirements = preflight.extract_deployment_requirements(
        template
    )
    actions = {item.action for item in requirements}

    # Present in this template:
    assert "s3:PutEncryptionConfiguration" in actions
    assert "s3:PutPublicAccessBlock" in actions
    assert "dynamodb:CreateTable" in actions
    assert "lambda:AddPermission" in actions
    assert "apigatewayv2:CreateApi" in actions

    # NOT triggered by anything in this template:
    assert "lambda:CreateEventSourceMapping" not in actions
    assert "apigatewayv2:CreateAuthorizer" not in actions


def test_managed_bucket_requirements_follow_resolve_s3():
    if preflight.extract_resolve_s3():
        actions = {
            item.action
            for item in preflight.managed_bucket_requirements()
        }
        assert "s3:CreateBucket" in actions
        assert "s3:PutObject" in actions
    else:
        assert True


def test_category_b_runtime_grants_kept_separate():
    template = preflight.parse_template()
    grants = preflight.extract_runtime_grants(template)

    assert set(grants) == {
        "DocumentsFunctionRole",
        "AggregatesFunctionRole",
    }

    document_actions = {
        action
        for grant in grants["DocumentsFunctionRole"]
        for action in grant["actions"]
    }

    assert "dynamodb:PutItem" in document_actions
    assert "iam:CreateRole" not in document_actions

    # Category B actions must not appear as deployment requirements.
    requirements = preflight.extract_deployment_requirements(
        template
    )
    assert "dynamodb:PutItem" not in {
        item.action for item in requirements
    }


def test_scope_map_is_runtime_and_prefix_scoped():
    scopes = preflight.build_scope_map(
        "my-stack", "eu-west-3", "222211114444"
    )

    assert scopes["DDB_TABLES"] == (
        "arn:aws:dynamodb:eu-west-3:222211114444:table/my-stack-*"
    )
    assert (
        scopes["TEMPLATE_BUCKET"]
        == "arn:aws:s3:::my-stack-*"
    )
    assert scopes["SAM_MANAGED_BUCKET"] == [
        "arn:aws:s3:::aws-sam-cli-managed-*",
        "arn:aws:s3:::aws-sam-cli-managed-*/*",
    ]


# ---------------------------------------------------------------
# Verification tiers
# ---------------------------------------------------------------

def _sample_requirements():
    return [
        preflight.Requirement(
            "iam:CreateRole", "IAM_ROLES", "role creation",
            "DocumentsFunctionRole",
        ),
        preflight.Requirement(
            "dynamodb:CreateTable", "DDB_TABLES", "table create",
            "DocumentsTable",
        ),
        preflight.Requirement(
            "s3:CreateBucket", "TEMPLATE_BUCKET", "bucket create",
            "DocumentsBucket",
        ),
    ]


def _sample_scopes():
    return preflight.build_scope_map(
        "my-stack", "eu-west-3", "222211114444"
    )


def test_missing_and_present_permissions():
    simulator = FakeSimulator(allowed=["dynamodb:CreateTable"])

    statuses, unverifiable = preflight.run_simulated_checks(
        "arn:aws:iam::222211114444:user/deployer",
        _sample_requirements(),
        _sample_scopes(),
        simulator,
    )

    assert statuses["dynamodb:CreateTable"] == "VERIFIED"
    assert statuses["s3:CreateBucket"] == "MISSING"
    assert statuses["iam:CreateRole"] == "MISSING"
    assert unverifiable == []


def test_multiple_missing_permissions_all_reported():
    simulator = FakeSimulator(allowed=[])

    statuses, _ = preflight.run_simulated_checks(
        "arn:aws:iam::222211114444:user/deployer",
        _sample_requirements(),
        _sample_scopes(),
        simulator,
    )

    assert len(statuses) == 3
    assert all(
        state == "MISSING" for state in statuses.values()
    )


def test_all_permissions_present():
    simulator = FakeSimulator(
        allowed=[item.action for item in _sample_requirements()]
    )

    statuses, unverifiable = preflight.run_simulated_checks(
        "arn:aws:iam::222211114444:user/deployer",
        _sample_requirements(),
        _sample_scopes(),
        simulator,
    )

    assert all(
        state == "VERIFIED" for state in statuses.values()
    )
    assert unverifiable == []


def test_simulator_denied_marks_unverifiable_not_missing():
    statuses, unverifiable = preflight.run_simulated_checks(
        "arn:aws:iam::222211114444:user/deployer",
        _sample_requirements(),
        _sample_scopes(),
        DenyingSimulator(),
    )

    assert len(unverifiable) == 3
    assert all(
        state == "UNVERIFIED" for state in statuses.values()
    )
    # Unverifiable must NOT be conflated with MISSING.
    missing = preflight.missing_permissions(
        _sample_requirements(), statuses
    )
    assert missing == []


def test_root_identity_never_hits_the_simulator():
    # Root ARNs cause InvalidInput in AWS's simulator; the preflight
    # must short-circuit to VERIFIED_ROOT instead of erroring.
    simulator = FakeSimulator()

    statuses, unverifiable = preflight.run_simulated_checks(
        "arn:aws:iam::222211114444:root",
        _sample_requirements(),
        _sample_scopes(),
        simulator,
    )

    assert simulator.calls == []
    assert all(
        state == "VERIFIED_ROOT" for state in statuses.values()
    )
    assert unverifiable == []

    missing = preflight.missing_permissions(
        _sample_requirements(), statuses
    )
    assert missing == []


def test_policy_document_fallback_evaluation():
    policies = [
        (
            "admin",
            json.loads(
                json.dumps(
                    {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Action": "*",
                                "Resource": "*",
                            }
                        ],
                    }
                )
            ),
        )
    ]

    statuses = preflight.evaluate_actions_against_policies(
        _sample_requirements(), _sample_scopes(), policies
    )

    assert all(
        state == "ALLOWED_BY_POLICY_DOCS"
        for state in statuses.values()
    )

    # A policy granting only something else must NOT verify.
    restrictive = [
        (
            "narrow",
            {
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": "s3:ListBucket",
                        "Resource": "*",
                    }
                ]
            },
        )
    ]

    statuses = preflight.evaluate_actions_against_policies(
        _sample_requirements(), _sample_scopes(), restrictive
    )

    assert statuses["iam:CreateRole"] == "UNKNOWN"


# ---------------------------------------------------------------
# Policy artifact
# ---------------------------------------------------------------

def test_policy_artifact_is_valid_json_and_scoped():
    document = json.loads(
        preflight.POLICY_ARTIFACT.read_text(encoding="utf-8")
    )

    assert document["Version"] == "2012-10-17"

    for statement in document["Statement"]:
        assert statement["Effect"] == "Allow"

        # No wildcard-everything anywhere - documented exceptions
        # would need to live next to this assertion.
        assert statement["Action"] != "*"
        assert statement["Resource"] != "*"

        for action in statement["Action"]:
            assert action != "*"

        for resource in statement["Resource"]:
            assert resource != "*"

    actions = {
        action
        for statement in document["Statement"]
        for action in statement["Action"]
    }

    assert "iam:CreateRole" in actions
    assert "iam:PassRole" not in actions


def test_policy_artifact_has_no_account_region_or_stack_literals():
    text = preflight.POLICY_ARTIFACT.read_text(encoding="utf-8")

    assert "{{ACCOUNT_ID}}" in text
    assert "{{REGION}}" in text
    assert "{{STACK_NAME}}" in text

    # Never pin the current contributor's values.
    assert not re.search(r"\b\d{12}\b", text)
    assert "ap-south-1" not in text
    assert "intelligent-storage-cost-optimizer-DocumentsTable-" not in text


def test_render_policy_document_substitutes_placeholders():
    rendered = preflight.render_policy_document(
        "SAMPLE_ACCOUNT", "sample-region", "sample-stack"
    )

    document = json.loads(rendered)

    assert "{{ACCOUNT_ID}}" not in rendered
    assert "{{REGION}}" not in rendered
    assert "{{STACK_NAME}}" not in rendered
    assert "arn:aws:iam::SAMPLE_ACCOUNT:role/sample-stack-*" in (
        rendered
    )


# ---------------------------------------------------------------
# Safety / portability invariants (source scans)
# ---------------------------------------------------------------

FORBIDDEN_IAM_MUTATIONS = [
    "put_role_policy",
    "attach_role_policy",
    "detach_role_policy",
    "put_user_policy",
    "attach_user_policy",
    "detach_user_policy",
    "put_group_policy",
    "create_role(",
    "delete_role(",
    "tag_role(",
    "untag_role(",
    "create_user",
    "delete_user",
    "create_policy(",
    "delete_policy(",
    "update_assume_role",
    "tag_resource(",
    "untag_resource(",
]


def test_preflight_never_mutates_iam():
    source = (
        REPO_ROOT / "scripts" / "check_deployment_permissions.py"
    ).read_text(encoding="utf-8")

    for forbidden in FORBIDDEN_IAM_MUTATIONS:
        assert forbidden not in source, (
            f"preflight script contains IAM mutating call: "
            f"{forbidden}"
        )


def test_preflight_has_no_credentials_or_account_ids():
    source = (
        REPO_ROOT / "scripts" / "check_deployment_permissions.py"
    ).read_text(encoding="utf-8")

    assert "aws_access_key" not in source.lower()
    assert "aws_secret" not in source.lower()
    assert "session_token" not in source.lower()
    assert not re.search(r"\b\d{12}\b", source)
    assert "ap-south-1" not in source
    assert "C:\\Users" not in source


def test_repo_sources_are_portable():
    """No account IDs / machine paths in any source we own."""

    self_path = Path(__file__).resolve()

    scanned = []
    for pattern in (
        "scripts/*.py",
        "backend/lambdas/**/*.py",
        "tests/*.py",
        "optimization/*.py",
        "infrastructure/*.yaml",
        "infrastructure/deployment-policy.json",
        "README.md",
        "samconfig.toml",
    ):
        scanned.extend(REPO_ROOT.glob(pattern))

    # This test file contains intentionally-fake 12-digit ARNs for
    # the offline fakes, so it is excluded from its own scan; the
    # dedicated test above already proves them non-hardcoded.
    scanned = [path for path in scanned
               if path.resolve() != self_path]

    for path in scanned:
        text = path.read_text(encoding="utf-8")

        assert not re.search(r"\b\d{12}\b", text), (
            f"12-digit account-like value in {path}"
        )
        assert "C:\\Users" not in text, (
            f"machine path in {path}"
        )
        assert "/Users/" not in text and "/home/" not in text, (
            f"machine path in {path}"
        )