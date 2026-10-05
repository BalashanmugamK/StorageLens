"""
AWS DEPLOYMENT PREFLIGHT for this repository.

Diagnoses whether the CURRENT deployment identity (user, role, or
session performing `sam deploy`) appears to have the IAM permissions
required by the CURRENT infrastructure/template.yaml and
samconfig.toml - before running the deployment.

Design rules:
  - Read-only. This tool NEVER creates, modifies, attaches, detaches,
    deletes, or broadens any IAM policy, role, or user. It only calls
    read-only APIs (sts, iam reads, iam policy simulator).
  - Nothing account/region/username specific is hardcoded: the stack
    name and region default come from samconfig.toml and are
    overridable on the command line; the account id and the identity
    ARN are always discovered at runtime via sts:GetCallerIdentity.
  - Permission requirements are extracted from the ACTUAL template
    (resource types + properties actually present) and from the
    ACTUAL samconfig (resolve_s3), not from a generic list.
  - Verification tiers, reported separately:
      VERIFIED       via the IAM Policy Simulator (strongest signal:
                     covers attached policies + permission boundaries)
      VERIFIED_BY_POLICY_DOCS
                     via reading the identity's inline/attached/group
                     policy documents (heuristic; misses boundaries)
      STATIC_ONLY    inferred from the template; not verifiable at
                     run time because IAM inspection was denied
      MISSING        simulator/policy review says it is lacking
    Neither tier covers:
      service control policies (organizational controls), session
      policies, resource policies, explicit SCP denies, or future
      CloudFormation internal readback calls. A clean preflight is
      strong evidence, not a guarantee.

  - A least-privilege deployment policy artifact lives at
    infrastructure/deployment-policy.json (placeholders
    {{ACCOUNT_ID}} / {{REGION}} / {{STACK_NAME}}). It is an artifact
    for an ADMINISTRATOR to review, substitute, and attach manually.
    This tool only renders a review copy into the gitignored
    experiments/results/ directory; it never touches IAM itself.

Categories (kept separate throughout the report):
  A. deployment-identity permissions - actions YOU need to run
     `sam deploy` (e.g. iam:CreateRole on the CFN-managed roles).
  B. Lambda runtime permissions - actions inside the two inline
     policy documents that CloudFormation grants to the execution
     roles of the deployed application (e.g. dynamodb:PutItem).
     The deployer does NOT need these.

Exit codes:
  0  no missing permissions found (verified or static inference)
  1  at least one missing permission was identified
  2  environment/identity resolution failed
  3  permissions could not be verified (IAM inspection denied);
     nothing known-missing, but the deployment is unproven
"""

import argparse
import fnmatch
import json
import re
import sys
import tomllib
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

TEMPLATE_FILE = REPO_ROOT / "infrastructure" / "template.yaml"
SAMCONFIG_FILE = REPO_ROOT / "samconfig.toml"
POLICY_ARTIFACT = REPO_ROOT / "infrastructure" / "deployment-policy.json"
RENDERED_POLICY = (
    REPO_ROOT / "experiments" / "results"
    / "deployment-policy.rendered.json"
)


# ---------------------------------------------------------------
# Requirement model
# ---------------------------------------------------------------

# Category A: every action is a deployment-identity requirement.

# CloudFormation stack lifecycle (always required by sam deploy).
CLOUDFORMATION_REQUIREMENTS = [
    ("cloudformation:CreateStack",
     "CFN_STACK",
     "first-time stack creation via sam deploy"),
    ("cloudformation:CreateChangeSet",
     "CFN_CHANGESET",
     "sam deploy prepares a change set for every deployment"),
    ("cloudformation:ExecuteChangeSet",
     "CFN_CHANGESET",
     "sam deploy applies the prepared change set"),
    ("cloudformation:DescribeChangeSet",
     "CFN_CHANGESET",
     "sam deploy inspects the change set it prepares"),
    ("cloudformation:DeleteChangeSet",
     "CFN_CHANGESET",
     "sam deploy discards failed/aborted change sets"),
    ("cloudformation:DescribeStacks",
     "CFN_STACK",
     "sam deploy checks current stack state before/after deploying"),
    ("cloudformation:DeleteStack",
     "CFN_STACK",
     "stack deletion and failed-create cleanup (sam delete)"),
]

# Category B: actions inside the Lambda execution-role inline
# policies (reported separately; the deployer does not need them).

ASSUME_ROLE_PRINCIPALS = ["lambda.amazonaws.com"]


@dataclass(frozen=True)
class Requirement:
    action: str
    scope: str          # key into the runtime scope map
    reason: str
    affected: str       # logical object(s) the action operates on


@dataclass(frozen=True)
class Status:
    action: str
    scope: str
    reason: str
    affected: str
    state: str          # MISSING / VERIFIED / VERIFIED_BY_POLICY_DOCS / STATIC_ONLY


def parse_template(path=TEMPLATE_FILE):
    """Parse template.yaml, neutralizing CloudFormation !-tags."""

    import yaml

    class TemplateLoader(yaml.SafeLoader):
        pass

    def _untag(loader, tag_suffix, node):
        if isinstance(node, yaml.ScalarNode):
            return loader.construct_scalar(node)
        if isinstance(node, yaml.SequenceNode):
            return loader.construct_sequence(node)
        return loader.construct_mapping(node, deep=True)

    TemplateLoader.add_multi_constructor("!", _untag)

    with path.open("r", encoding="utf-8") as file:
        return yaml.load(file, Loader=TemplateLoader)


def extract_deployment_requirements(template):
    """
    Derive Category A requirements from resource types AND the
    properties actually present in this template.
    """

    requirements = []

    for action, scope, reason in CLOUDFORMATION_REQUIREMENTS:
        requirements.append(
            Requirement(
                action, scope, reason,
                affected="CloudFormation stack "
                         "(all template resources)",
            )
        )

    resources = template.get("Resources", {})

    for logical_id, resource in resources.items():
        rtype = resource.get("Type", "")
        props = resource.get("Properties") or {}

        if rtype == "AWS::S3::Bucket":
            requirements.append(
                Requirement(
                    "s3:CreateBucket", "TEMPLATE_BUCKET",
                    "create the documents bucket",
                    affected=logical_id,
                )
            )
            if props.get("BucketEncryption"):
                requirements.append(
                    Requirement(
                        "s3:PutEncryptionConfiguration",
                        "TEMPLATE_BUCKET",
                        "the bucket declares SSE-AES256 "
                        "encryption",
                        affected=logical_id,
                    )
                )
            if props.get("PublicAccessBlockConfiguration"):
                requirements.append(
                    Requirement(
                        "s3:PutPublicAccessBlock",
                        "TEMPLATE_BUCKET",
                        "the bucket declares a full "
                        "public-access block",
                        affected=logical_id,
                    )
                )
            requirements.append(
                Requirement(
                    "s3:GetEncryptionConfiguration",
                    "TEMPLATE_BUCKET",
                    "CloudFormation readback of the declared "
                    "encryption configuration",
                    affected=logical_id,
                )
            )
            requirements.append(
                Requirement(
                    "s3:GetPublicAccessBlock",
                    "TEMPLATE_BUCKET",
                    "CloudFormation readback of the declared "
                    "public-access block",
                    affected=logical_id,
                )
            )
            requirements.append(
                Requirement(
                    "s3:DeleteBucket", "TEMPLATE_BUCKET",
                    "bucket rollback/cleanup",
                    affected=logical_id,
                )
            )

        elif rtype == "AWS::DynamoDB::Table":
            for action, reason in [
                ("dynamodb:CreateTable",
                 "create the table"),
                ("dynamodb:DescribeTable",
                 "CloudFormation readback during create/update"),
                ("dynamodb:DeleteTable",
                 "table rollback/cleanup"),
                ("dynamodb:TagResource",
                 "stack-tag propagation onto taggable tables"),
            ]:
                requirements.append(
                    Requirement(
                        action, "DDB_TABLES", reason,
                        affected=logical_id,
                    )
                )

        elif rtype == "AWS::Serverless::Function":
            requirements.extend(
                [
                    Requirement(
                        "lambda:CreateFunction",
                        "LAMBDA_FUNCTIONS",
                        "create the function",
                        affected=logical_id,
                    ),
                    Requirement(
                        "lambda:UpdateFunctionCode",
                        "LAMBDA_FUNCTIONS",
                        "code updates on re-deploy",
                        affected=logical_id,
                    ),
                    Requirement(
                        "lambda:UpdateFunctionConfiguration",
                        "LAMBDA_FUNCTIONS",
                        "configuration updates on re-deploy",
                        affected=logical_id,
                    ),
                    Requirement(
                        "lambda:GetFunction",
                        "LAMBDA_FUNCTIONS",
                        "CloudFormation readback of function "
                        "state",
                        affected=logical_id,
                    ),
                    Requirement(
                        "lambda:DeleteFunction",
                        "LAMBDA_FUNCTIONS",
                        "function rollback/cleanup",
                        affected=logical_id,
                    ),
                ]
            )

            if props.get("Events"):
                requirements.append(
                    Requirement(
                        "lambda:AddPermission",
                        "LAMBDA_FUNCTIONS",
                        "API Gateway invoke grant created for "
                        "the function events",
                        affected=logical_id,
                    )
                )

            requirements.append(
                Requirement(
                    "lambda:TagResource",
                    "LAMBDA_FUNCTIONS",
                    "stack-tag propagation onto taggable "
                    "functions",
                    affected=logical_id,
                )
            )

            if not props.get("Role"):
                # Default role creation: CloudFormation builds an
                # execution role and its inline policy from this
                # function's Policies property.
                role_requirements = [
                    ("iam:CreateRole",
                     "CloudFormation must create the Lambda "
                     "execution role"),
                    ("iam:GetRole",
                     "CloudFormation reads the role it manages "
                     "on updates"),
                    ("iam:PutRolePolicy",
                     "the execution role's inline policy comes "
                     "from this function's Policies property"),
                    ("iam:GetRolePolicy",
                     "CloudFormation compares the inline policy "
                     "on updates"),
                    ("iam:DeleteRolePolicy",
                     "inline-policy replacement and rollback"),
                    ("iam:DeleteRole",
                     "role cleanup"),
                    ("iam:TagRole",
                     "role tagging during creation/update "
                     "(stack tags and AWS-managed auto-tagging)"),
                    ("iam:UntagRole",
                     "role tag updates"),
                ]
                for action, reason in role_requirements:
                    requirements.append(
                        Requirement(
                            action, "IAM_ROLES", reason,
                            affected=f"{logical_id}Role",
                        )
                    )

        elif rtype == "AWS::Serverless::HttpApi":
            for action in [
                "apigatewayv2:CreateApi",
                "apigatewayv2:GetApi",
                "apigatewayv2:UpdateApi",
                "apigatewayv2:DeleteApi",
                "apigatewayv2:CreateStage",
                "apigatewayv2:GetStage",
                "apigatewayv2:UpdateStage",
                "apigatewayv2:CreateIntegration",
                "apigatewayv2:GetIntegration",
                "apigatewayv2:UpdateIntegration",
                "apigatewayv2:CreateRoute",
                "apigatewayv2:GetRoute",
                "apigatewayv2:UpdateRoute",
            ]:
                requirements.append(
                    Requirement(
                        action, "APIS",
                        "HTTP API ($default stage) lifecycle "
                        "managed by CloudFormation",
                        affected=logical_id,
                    )
                )

    return requirements


def extract_resolve_s3(samconfig_file=SAMCONFIG_FILE):
    """True when samconfig asks SAM to manage the deployment bucket."""

    with samconfig_file.open("rb") as file:
        config = tomllib.load(file)

    parameters = (
        config.get("default", {})
        .get("deploy", {})
        .get("parameters", {})
    )

    return bool(parameters.get("resolve_s3"))


def load_deployment_config(samconfig_file=SAMCONFIG_FILE):
    """Stack name / region / resolve_s3 defaults from samconfig."""

    with samconfig_file.open("rb") as file:
        config = tomllib.load(file)

    defaults = config.get("default", {})
    deploy_parameters = defaults.get("deploy", {}).get(
        "parameters", {}
    )
    global_parameters = defaults.get("global", {}).get(
        "parameters", {}
    )

    return {
        "stack_name": deploy_parameters.get("stack_name"),
        "region": global_parameters.get("region")
        or deploy_parameters.get("region"),
        "resolve_s3": bool(deploy_parameters.get("resolve_s3")),
    }


def managed_bucket_requirements():
    """SAM-managed deployment bucket actions (resolve_s3 = true)."""

    return [
        Requirement(
            "s3:CreateBucket", "SAM_MANAGED_BUCKET",
            "sam deploy creates a managed bucket to hold "
            "build artifacts when resolve_s3 is set "
            "(reuses one if it already exists)",
            affected="SAM-managed deployment bucket",
        ),
        Requirement(
            "s3:GetBucketLocation", "SAM_MANAGED_BUCKET",
            "sam deploy locates the managed deployment bucket",
            affected="SAM-managed deployment bucket",
        ),
        Requirement(
            "s3:PutObject", "SAM_MANAGED_BUCKET",
            "sam deploy uploads the build and template "
            "artifacts",
            affected="SAM-managed deployment bucket",
        ),
        Requirement(
            "s3:GetObject", "SAM_MANAGED_BUCKET",
            "CloudFormation reads the uploaded artifacts",
            affected="SAM-managed deployment bucket",
        ),
        Requirement(
            "s3:ListBucket", "SAM_MANAGED_BUCKET",
            "sam deploy verifies artifact presence",
            affected="SAM-managed deployment bucket",
        ),
        Requirement(
            "s3:DeleteObject", "SAM_MANAGED_BUCKET",
            "sam deploy artifact cleanup",
            affected="SAM-managed deployment bucket",
        ),
    ]


def extract_runtime_grants(template):
    """
    Category B: the inline policy documents CloudFormation grants to
    each Lambda execution role. Extracted verbatim from the template.
    """

    def _as_list(value):
        return value if isinstance(value, list) else [value]

    runtime_grants = {}

    for logical_id, resource in (
        template.get("Resources", {}).items()
    ):
        if resource.get("Type") != "AWS::Serverless::Function":
            continue

        grants = []

        for policy in (
            resource.get("Properties", {}).get("Policies") or []
        ):
            # SAM accepts a list of policy documents (dicts with a
            # Statement list) or plain managed-policy names (str).
            if isinstance(policy, str):
                grants.append(
                    {
                        "actions": [f"(managed policy: {policy})"],
                        "resources": [],
                    }
                )
                continue

            statements = policy.get("Statement", [])

            if isinstance(statements, dict):
                statements = [statements]

            for statement in statements:
                if statement.get("Effect") != "Allow":
                    continue
                grants.append(
                    {
                        "actions": _as_list(
                            statement.get("Action", [])
                        ),
                        "resources": statement.get("Resource", []),
                    }
                )

        if grants:
            runtime_grants[f"{logical_id}Role"] = grants

    return runtime_grants


# ---------------------------------------------------------------
# Runtime identity + verification
# ---------------------------------------------------------------

def simulate_source_arn(identity_arn):
    """
    The Policy Simulator needs a principal ARN (user or role), not a
    session ARN. Convert assumed-role session ARNs to their role ARN.
    """

    match = re.match(
        r"arn:([^:]+):sts::([^:]+):assumed-role/([^/]+)/",
        identity_arn,
    )

    if match:
        partition, account, role_name = match.groups()
        return (
            f"arn:{partition}:iam::{account}:role/{role_name}"
        )

    return identity_arn


def describe_identity(identity):
    """Human-readable identity description."""

    arn = identity["Arn"]

    if ":assumed-role/" in arn:
        return "assumed role session"
    if ":user/" in arn:
        return "IAM user"
    if ":federated-user/" in arn:
        return "federated user"
    if arn.endswith(":root"):
        return "account root user (not recommended for deployments)"

    return f"identity ({arn})"


def resolve_identity(region):
    """Discover runtime identity/account. No hardcoded identifiers."""

    import boto3

    sts = boto3.client("sts", region_name=region)

    return sts.get_caller_identity()


def build_scope_map(stack_name, region, account):
    """
    Narrow resource scopes for every requirement. Built entirely
    from runtime-discovered values: stack prefix (CloudFormation
    generated physical names start with the stack name, S3 lower-
    cases theirs; DynamoDB/Lambda/IAM keep case) plus the
    documented SAM-managed bucket prefix.
    """

    stack_prefix = stack_name.lower()

    return {
        "CFN_STACK": (
            f"arn:aws:cloudformation:{region}:{account}"
            f":stack/{stack_name}/*"
        ),
        "CFN_CHANGESET": (
            f"arn:aws:cloudformation:{region}:{account}"
            f":changeSet/{stack_name}/*"
        ),
        "SAM_MANAGED_BUCKET": [
            "arn:aws:s3:::aws-sam-cli-managed-*",
            "arn:aws:s3:::aws-sam-cli-managed-*/*",
        ],
        "TEMPLATE_BUCKET": (
            f"arn:aws:s3:::{stack_prefix}-*"
        ),
        "DDB_TABLES": (
            f"arn:aws:dynamodb:{region}:{account}"
            f":table/{stack_name}-*"
        ),
        "LAMBDA_FUNCTIONS": (
            f"arn:aws:lambda:{region}:{account}"
            f":function:{stack_name}-*"
        ),
        "IAM_ROLES": (
            f"arn:aws:iam::{account}:role/{stack_name}-*"
        ),
        "APIS": (
            f"arn:aws:apigateway:{region}::/apis*"
        ),
    }


def run_simulated_checks(
    caller_arn, requirements, scope_map, simulator,
):
    """
    Verify requirements with the IAM Policy Simulator.

    simulator only needs .simulate_principal_policy(**kwargs) -
    injectable so tests can supply fakes.

    Returns (statuses dict action -> state string, unverifiable list).

    The simulator cannot simulate the account ROOT user (AWS returns
    InvalidInput for root ARNs). Root has no IAM policies to
    simulate - all in-account actions are intrinsically permitted,
    so root identities are reported explicitly as VERIFIED_ROOT and
    never reach a confusing API error.
    """

    from botocore.exceptions import ClientError

    if caller_arn.endswith(":root"):
        return (
            {
                requirement.action: "VERIFIED_ROOT"
                for requirement in requirements
            },
            [],
        )

    source_arn = simulate_source_arn(caller_arn)

    grouped = defaultdict(list)

    for requirement in requirements:
        grouped[requirement.scope].append(requirement)

    statuses = {}
    unverifiable = []

    denied_codes = (
        "AccessDenied",
        "AccessDeniedException",
        "UnauthorizedOperation",
        "UnauthorizedAccessException",
    )

    for scope, group in grouped.items():
        actions = [
            requirement.action for requirement in group
        ]

        scopes = scope_map.get(scope, ["*"])
        resource_arns = (
            scopes if isinstance(scopes, list) else [scopes]
        )

        try:
            response = simulator.simulate_principal_policy(
                PolicySourceArn=source_arn,
                ActionNames=actions,
                ResourceArns=resource_arns,
            )
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code", "")
            if code in denied_codes:
                unverifiable.extend(actions)

                for requirement in group:
                    statuses[requirement.action] = "UNVERIFIED"
                continue

            raise

        decisions = {
            result["EvalActionName"]: result["EvalDecision"]
            for result in response.get("EvaluationResults", [])
        }

        for requirement in group:
            decision = decisions.get(requirement.action)

            if decision in ("allowed", "explicitAllow"):
                statuses[requirement.action] = "VERIFIED"
            elif decision == "explicitDeny":
                statuses[requirement.action] = "MISSING"
            else:
                statuses[requirement.action] = "MISSING"

    return statuses, unverifiable


# ---------------------------------------------------------------
# Fallback: review the identity's own policy documents (read-only)
# ---------------------------------------------------------------

def collect_identity_policies(iam_client, identity_arn):
    """
    Best-effort collection of inline + attached policies (- and, for
    IAM users, group policies) for THIS identity only. Read-only.
    Raises PermissionError when IAM inspection is denied.
    """

    from botocore.exceptions import ClientError

    documents = []

    def _deny(error):
        return error.response.get("Error", {}).get("Code", "") in (
            "AccessDenied",
            "AccessDeniedException",
            "NoSuchEntity",
        )

    def _attached(scope_list):
        try:
            attached = scope_list()
        except ClientError as error:
            if _deny(error):
                raise PermissionError(
                    "IAM policy listing denied"
                )
            raise

        for policy in attached.get("AttachedPolicies", []):
            try:
                policy_meta = iam_client.get_policy(
                    PolicyArn=policy["PolicyArn"]
                )["Policy"]
                version = iam_client.get_policy_version(
                    PolicyArn=policy["PolicyArn"],
                    VersionId=policy_meta["DefaultVersionId"],
                )["PolicyVersion"]["Document"]
                documents.append(
                    (
                        policy["PolicyName"],
                        _normalize_policy_document(version),
                    )
                )
            except ClientError as error:
                if _deny(error):
                    raise PermissionError(
                        "IAM policy retrieval denied"
                    )
                raise

    def _inline(scope_get, scope_list):
        try:
            names = scope_list()
        except ClientError as error:
            if _deny(error):
                raise PermissionError(
                    "IAM policy listing denied"
                )
            raise

        for name in names.get("PolicyNames", []):
            try:
                result = scope_get(name)
                documents.append(
                    (
                        name,
                        _normalize_policy_document(
                            result["PolicyDocument"]
                        ),
                    )
                )
            except ClientError as error:
                if _deny(error):
                    raise PermissionError(
                        "IAM policy retrieval denied"
                    )
                raise

    user_match = re.search(r":user/(.+)$", identity_arn)
    role_session = re.search(r":assumed-role/([^/]+)/", identity_arn)
    role_direct = re.search(r":role/(.+)$", identity_arn)

    if user_match:
        user_name = user_match.group(1)

        _attached(
            lambda: iam_client.list_attached_user_policies(
                UserName=user_name
            ),
        )
        _inline(
            lambda name: iam_client.get_user_policy(
                UserName=user_name, PolicyName=name
            ),
            lambda: iam_client.list_user_policies(
                UserName=user_name
            ),
        )

        # Group policies too (best effort).
        try:
            groups = iam_client.list_groups_for_user(
                UserName=user_name
            )
        except ClientError as error:
            if _deny(error):
                raise PermissionError(
                    "IAM group listing denied"
                )
            raise

        for group in groups.get("Groups", []):
            group_name = group["GroupName"]

            _attached(
                lambda group_name=group_name: (
                    iam_client.list_attached_group_policies(
                        GroupName=group_name
                    )
                ),
            )
            _inline(
                lambda name, group_name=group_name: (
                    iam_client.get_group_policy(
                        GroupName=group_name, PolicyName=name
                    )
                ),
                lambda group_name=group_name: (
                    iam_client.list_group_policies(
                        GroupName=group_name
                    )
                ),
            )

    elif role_session or role_direct:
        role_name = (
            role_session.group(1) if role_session
            else role_direct.group(1)
        )

        _attached(
            lambda: iam_client.list_attached_role_policies(
                RoleName=role_name
            ),
        )
        _inline(
            lambda name: iam_client.get_role_policy(
                RoleName=role_name, PolicyName=name
            ),
            lambda: iam_client.list_role_policies(
                RoleName=role_name
            ),
        )

    else:
        raise PermissionError(
            "unsupported identity type for policy review: "
            f"{identity_arn}"
        )

    return documents


def _normalize_policy_document(document):
    """URL-decoded policy docs (API responses) stay as given."""

    if isinstance(document, str):
        return json.loads(document)

    return document


def _statement_actions(statement):
    actions = statement.get("Action", [])

    if isinstance(actions, str):
        actions = [actions]

    return actions


def _statement_resources(statement):
    resources = statement.get("Resource", ["*"])

    if isinstance(resources, str):
        resources = [resources]

    return resources


def evaluate_actions_against_policies(
    requirements, scope_map, policies,
):
    """
    Heuristic check: allow if SOME collected policy statement
    (Allow, non-NotAction) matches action pattern AND resource
    pattern. Cannot evaluate boundaries/SCPs/session policies.
    """

    statuses = {}

    for requirement in requirements:
        scope = scope_map.get(requirement.scope, ["*"])
        resource_arns = (
            scope if isinstance(scope, list) else [scope]
        )

        allowed = False

        for _, document in policies:
            for statement in document.get("Statement", []):
                if statement.get("Effect") != "Allow":
                    continue
                if "NotAction" in statement or "NotResource" in statement:
                    continue

                if not any(
                    fnmatch.fnmatch(
                        requirement.action.lower(),
                        action.lower(),
                    )
                    for action in _statement_actions(statement)
                ):
                    continue

                statement_resources = _statement_resources(statement)

                if any(
                    fnmatch.fnmatch(arn, pattern)
                    for arn in resource_arns
                    for pattern in statement_resources
                ) or "*" in statement_resources:
                    allowed = True
                    break

            if allowed:
                break

        statuses[requirement.action] = (
            "ALLOWED_BY_POLICY_DOCS" if allowed else "UNKNOWN"
        )

    return statuses


# ---------------------------------------------------------------
# Policy artifact rendering
# ---------------------------------------------------------------

def render_policy_document(account, region, stack_name):
    """
    Render the committed least-privilege artifact with the values
    discovered at runtime. Values come from runtime discovery only.
    """

    text = POLICY_ARTIFACT.read_text(encoding="utf-8")

    rendered = (
        text.replace("{{ACCOUNT_ID}}", account)
        .replace("{{REGION}}", region)
        .replace("{{STACK_NAME}}", stack_name)
    )

    return rendered


def save_rendered_policy(rendered):
    RENDERED_POLICY.parent.mkdir(parents=True, exist_ok=True)

    RENDERED_POLICY.write_text(
        rendered if rendered.strip().startswith("{")
        else rendered,
        encoding="utf-8",
    )

    return RENDERED_POLICY


# ---------------------------------------------------------------
# Report
# ---------------------------------------------------------------

SYMBOLS = {
    "VERIFIED": "[OK]     ",
    "VERIFIED_ROOT": "[OK!]    ",
    "VERIFIED_BY_POLICY_DOCS": "[OK*]    ",
    "ALLOWED_BY_POLICY_DOCS": "[OK*]    ",
    "STATIC_ONLY": "[STATIC] ",
    "UNVERIFIED": "[UNVER?] ",
    "MISSING": "[MISSING]",
}


def symbol(state):
    return SYMBOLS.get(state, "[? ]     ")


def state_note(state):
    return {
        "VERIFIED": "verified with the IAM Policy Simulator",
        "VERIFIED_ROOT": (
            "root identity - intrinsically permitted "
            "(see warning above)"
        ),
        "VERIFIED_BY_POLICY_DOCS": (
            "permitted according to the identity's reviewable policy "
            "documents (heuristic - boundaries/SCPs not evaluated)"
        ),
        "ALLOWED_BY_POLICY_DOCS": (
            "permitted according to the identity's reviewable policy "
            "documents (heuristic - boundaries/SCPs not evaluated)"
        ),
        "STATIC_ONLY": (
            "inferred from the template; IAM inspection was not "
            "possible to confirm this"
        ),
        "UNVERIFIED": (
            "could not be verified - IAM inspection was denied for "
            "this identity"
        ),
        "MISSING": "not granted - deployment will likely fail here",
    }.get(state, "unknown verification state")


def print_report(
    identity_description, identity_arn, account, region, stack_name,
    requirements, statuses, runtime_grants, missing, unverifiable,
):
    print("=" * 64)
    print("AWS DEPLOYMENT PREFLIGHT")
    print("=" * 64)
    print()
    print("Deployment identity:")
    print(f"  {identity_description}")
    print(f"  {identity_arn}")
    print()
    print(f"AWS account:        {account}")
    print(f"Region:             {region}")
    print(f"Stack:              {stack_name}")

    if identity_arn.endswith(":root"):
        print()
        print(
            "  WARNING: the deployment identity is the ACCOUNT "
            "ROOT user."
        )
        print(
            "  There are no IAM policies to evaluate; aside from"
        )
        print(
            "  organizational controls (SCPs), all in-account "
            "actions"
        )
        print(
            "  are intrinsically permitted, so no missing "
            "permissions"
        )
        print(
            "  can be reported. Deploying as root is NOT "
            "recommended:"
        )
        print(
            "  an administrator should create a least-privilege"
        )
        print(
            "  deployment role/user instead - see the reviewable"
        )
        print(
            f"  policy at "
            f"{POLICY_ARTIFACT.relative_to(REPO_ROOT)}"
        )
    print()
    print("Required deployment permissions (Category A):")
    print()

    for requirement in requirements:
        state = statuses.get(requirement.action, "STATIC_ONLY")
        print(
            f"{symbol(state)} {requirement.action:<38}"
            f" -> {scope_label(requirement.scope)}"
        )
        print(
            f"           {state_note(state)}"
        )

    print()
    print("Category B - Lambda runtime permissions (granted to")
    print("EXECUTION ROLES by CloudFormation; NOT needed by the")
    print("deployment identity):")
    print()

    for role_name, grants in runtime_grants.items():
        actions = sorted(
            {action for grant in grants for action in grant["actions"]}
        )
        print(f"  {role_name}: {', '.join(actions)}")

    print()
    print("=" * 64)

    if missing:
        print(f"MISSING PERMISSIONS ({len(missing)}):")
        for action, requirement in missing:
            print(
                f"  - {action} "
                f"(required for {requirement.affected})"
            )
            print(
                f"      Reason: {requirement.reason}"
            )
        print()
        print("RESULT:")
        print("  [X] Deployment prerequisites INCOMPLETE")
        print()
        print("Recommended administrator action:")
        print(
            "  Grant the missing permissions to the deployment"
        )
        print(
            "  identity (or a deployment role with equivalent"
        )
        print(
            "  permissions). A least-privilege reviewable policy"
        )
        print(f"  is at: {POLICY_ARTIFACT.relative_to(REPO_ROOT)}")

    elif unverifiable:
        print("RESULT:")
        print(
            "  [~] Nothing known-missing, but permissions could "
            "NOT be verified"
        )
        print()
        print(
            "  The current identity lacks permission to inspect "
            "IAM"
        )
        print(
            "  (e.g. iam:SimulatePrincipalPolicy, or read of "
            "policies)."
        )
        print(
            "  All listed permissions are static template "
            "inferences."
        )
        print(
            "  `sam deploy` may still fail - most often on "
            "iam:CreateRole"
        )
        print("  for the Lambda execution roles.")
        print()
        print("Recommended administrator action:")
        print(
            "  either grant IAM inspection access, or review and"
        )
        print(
            "  attach the least-privilege policy at"
        )
        print(f"  {POLICY_ARTIFACT.relative_to(REPO_ROOT)}")

    else:
        print("RESULT:")
        print("  [OK] Deployment prerequisites look complete.")

    print()
    print("=" * 64)
    print(
        "NOT COVERED BY ANY CHECK (cannot be preflighted):"
    )
    print(
        "  - service control policies (organization controls)"
    )
    print("  - session policies")
    print("  - resource-based policies")
    print(
        "  - future changes to CloudFormation's internal "
        "readback calls"
    )
    print(
        "  - the exact generated physical names used by "
        "CloudFormation"
    )
    print(
        "    (patterns assume the documented stack-name prefix)"
    )
    print(
        "iam:PassRole is intentionally NOT required here:"
    )
    print(
        "  CloudFormation creates both execution roles inside "
        "the same"
    )
    print(
        "  stack and binds them itself - no PassRole applies."
    )
    print("=" * 64)

    return None


def scope_label(scope):
    return {
        "CFN_STACK": "CloudFormation stack",
        "CFN_CHANGESET": "CloudFormation change set",
        "SAM_MANAGED_BUCKET": "SAM-managed deployment bucket",
        "TEMPLATE_BUCKET": "documents bucket",
        "DDB_TABLES": "stack DynamoDB tables",
        "LAMBDA_FUNCTIONS": "stack Lambda functions",
        "IAM_ROLES": "stack execution roles",
        "APIS": "HTTP API",
    }.get(scope, scope)


def missing_permissions(requirements, statuses):
    """Collect (action, requirement) pairs that are MISSING."""

    missing = []

    for requirement in requirements:
        if statuses.get(requirement.action) == "MISSING":
            missing.append((requirement.action, requirement))

    # De-duplicate repeated actions across resource instances.
    seen = {}
    for action, requirement in missing:
        if action in seen:
            existing = seen[action]

            if requirement.affected not in existing["affected"]:
                existing["affected"] = (
                    f"{existing['affected']}, "
                    f"{requirement.affected}"
                )
            if requirement.reason not in existing["reasons"]:
                existing["reasons"].append(requirement.reason)
        else:
            seen[action] = {
                "affected": requirement.affected,
                "reasons": [requirement.reason],
            }

    rendered = []

    for requirement in requirements:
        for action in list(seen):
            if (
                action == requirement.action
                and requirement.action in seen
            ):
                entry = seen[action]

                rendered.append(
                    (
                        action,
                        _MergedRequirement(
                            action,
                            entry["affected"],
                            " / ".join(entry["reasons"]),
                        ),
                    )
                )
                del seen[action]

    return rendered


class _MergedRequirement(Requirement):
    """
    Lightweight merged view used for the missing-permission report.
    """

    def __init__(self, action, affected, reason):
        self.action = action
        self.affected = affected
        self.reason = reason
        self.scope = ""


# ---------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Read-only preflight: verify that the current AWS "
            "identity appears able to deploy this repository's SAM "
            "stack. Never modifies IAM."
        )
    )

    parser.add_argument(
        "--stack-name",
        default=None,
        help="override the samconfig stack-name default",
    )

    parser.add_argument(
        "--region",
        default=None,
        help="override the samconfig region default",
    )

    parser.add_argument(
        "--json",
        action="store_true",
        help="machine-readable summary on stdout",
    )

    args = parser.parse_args(argv)

    try:
        config = load_deployment_config()
    except Exception as error:
        print(f"ERROR: cannot read {SAMCONFIG_FILE}: {error}")
        return 2

    stack_name = args.stack_name or config["stack_name"]
    region = args.region or config["region"]

    if not stack_name or not region:
        print(
            "ERROR: stack name/region could not be resolved; "
            "pass --stack-name/--region.",
        )
        return 2

    template = parse_template()

    requirements = extract_deployment_requirements(template)

    if config["resolve_s3"]:
        requirements.extend(managed_bucket_requirements())

    # De-duplicate identical (action) requirements across the
    # report, keeping one entry per action.
    unique = {}
    for requirement in requirements:
        unique[requirement.action] = requirement

    requirements = list(
        sorted(
            unique.values(),
            key=lambda requirement: (
                requirement.scope,
                requirement.action,
            ),
        )
    )

    try:
        identity = resolve_identity(region)
    except Exception as error:
        print(
            f"ERROR: could not resolve the AWS identity for "
            f"region {region}: {error}"
        )
        return 2

    identity_arn = identity["Arn"]
    account = identity.get("Account", "")
    description = describe_identity(identity)

    scope_map = build_scope_map(stack_name, region, account)

    import boto3
    from botocore.exceptions import ClientError

    simulator = boto3.client("iam", region_name=region)

    try:
        statuses, unverifiable = run_simulated_checks(
            identity_arn, requirements, scope_map, simulator,
        )

        verification_level = (
            "ROOT"
            if any(
                state == "VERIFIED_ROOT"
                for state in statuses.values()
            )
            else "SIMULATOR"
        )
    except ClientError as error:
        code = error.response.get("Error", {}).get("Code", "")

        if code not in (
            "AccessDenied",
            "AccessDeniedException",
            "UnauthorizedAccessException",
            "UnauthorizedOperation",
        ):
            print(
                f"ERROR: IAM simulator call failed "
                f"unexpectedly: {error}",
                file=sys.stderr,
            )
            return 3

        # The identity cannot inspect IAM. Fall back to reading
        # its own policy documents (also read-only), or report the
        # clear limitation instead of failing mysteriously.
        try:
            iam_client = boto3.client("iam", region_name=region)
            policies = collect_identity_policies(
                iam_client, identity_arn,
            )
        except (PermissionError, ClientError):
            policies = None

        if not policies:
            statuses = {
                requirement.action: "STATIC_ONLY"
                for requirement in requirements
            }
            unverifiable = [
                requirement.action
                for requirement in requirements
            ]
            verification_level = "DENIED_IAM_INSPECTION"
        else:
            statuses = evaluate_actions_against_policies(
                requirements, scope_map, policies,
            )
            verification_level = "POLICY_DOCS"
            unverifiable = [
                action
                for action, state in statuses.items()
                if state in ("UNKNOWN",)
            ]

    missing = missing_permissions(requirements, statuses)

    runtime_grants = extract_runtime_grants(template)

    if args.json:
        print(
            json.dumps(
                {
                    "identity_arn": identity_arn,
                    "account": account,
                    "region": region,
                    "stack_name": stack_name,
                    "verification_level": verification_level,
                    "statuses": statuses,
                    "missing": [
                        action for action, _ in missing
                    ],
                    "unverifiable": unverifiable,
                    "runtime_grants": runtime_grants,
                },
                indent=2,
                default=str,
            )
        )
    else:
        print_report(
            description, identity_arn, account, region,
            stack_name, requirements, statuses, runtime_grants,
            missing, unverifiable,
        )

    # Render the least-privilege policy review copy (never any IAM
    # modification; output goes to a gitignored review location).
    if account and re.fullmatch(r"\d{12}", account or ""):
        rendered = render_policy_document(
            account, region, stack_name,
        )
        destination = save_rendered_policy(rendered)

        if not args.json:
            print(
                "Least-privilege deployment policy review copy "
                f"(gitignored): {destination}"
            )

    if missing:
        return 1

    if unverifiable:
        return 3

    return 0


if __name__ == "__main__":
    sys.exit(main())