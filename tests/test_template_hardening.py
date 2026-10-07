"""Structural tests for infrastructure/template.yaml hardening.

These pin the operational-hardening decisions made in the product
audit (Step 4 of the fix plan) against the ACTUAL template, so a
later edit cannot silently drop them:

  - daily EventBridge schedule for aggregation;
  - explicit log groups with retention instead of forever logs;
  - route throttling on the HTTP API;
  - alarms + SNS topic for failures;
  - the CloudWatch resource policy for API access logging.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_deployment_permissions as preflight


@pytest.fixture(scope="module")
def template():
    return preflight.parse_template()


FUNCTION_LOG_GROUPS = {
    "DocumentsFunctionLogGroup": "DocumentsFunction",
    "AggregatesFunctionLogGroup": "AggregatesFunction",
}


def test_aggregation_runs_on_a_daily_schedule(template):
    """The aggregates function must be scheduled; the documents
    function must NOT be (a per-upload aggregation schedule is
    pointless)."""

    resources = template["Resources"]

    events = (
        resources["AggregatesFunction"]["Properties"]["Events"]
    )

    schedules = [
        event["Properties"]["Schedule"]
        for event in events.values()
        if event["Type"] == "Schedule"
    ]

    assert "rate(1 day)" in schedules

    documents_events = (
        resources["DocumentsFunction"]["Properties"]["Events"]
    )

    assert all(
        event["Type"] != "Schedule"
        for event in documents_events.values()
    )


def test_function_logs_are_retained_groups(template):
    resources = template["Resources"]

    for group_id, function_id in FUNCTION_LOG_GROUPS.items():
        group = resources[group_id]["Properties"]

        # Retention set, and the group keeps the /aws/lambda/
        # prefix so CloudWatch console filters still match.
        assert group["RetentionInDays"] >= 30
        assert "/aws/lambda/" in group["LogGroupName"]

        # The group name must NOT reference the function: the
        # function's LoggingConfig refs back to this group, which
        # would form a circular dependency (SAM lint E3004).
        function_physical_names = {"DocumentsFunction", "AggregatesFunction"}
        group_name_refs = str(group["LogGroupName"])
        refs = {
            fn for fn in function_physical_names
            if f"${{{fn}}}" in str(group_name_refs)
        }
        assert not refs, (
            f"{group_id} LogGroupName references {refs}, forming a "
            "circular dependency with its LoggingConfig"
        )

        fn = resources[function_id]["Properties"]

        assert fn["LoggingConfig"]["LogGroup"] == group_id


def test_api_logs_expired_after_ninety_days_and_granted(template):
    resources = template["Resources"]

    access_logs = resources["ApiAccessLogs"]["Properties"]

    assert access_logs["RetentionInDays"] >= 90
    assert "/aws/apigateway/" in access_logs["LogGroupName"]

    # The API stage writes these logs under the API Gateway
    # service principal, hence the resource policy.
    policies = [
        resource
        for resource in resources.values()
        if resource["Type"] == "AWS::Logs::ResourcePolicy"
    ]

    assert policies, "API access-log resource policy missing"


def test_api_stage_throttles_default_routes(template):
    settings = (
        template["Resources"]["DocumentsHttpApi"]["Properties"][
            "DefaultRouteSettings"
        ]
    )

    assert settings["ThrottlingRateLimit"] >= 1
    assert settings["ThrottlingBurstLimit"] >= 1

    # Burst must exceed the sustained rate, or API Gateway
    # rejects the value.
    assert (
        settings["ThrottlingBurstLimit"]
        >= settings["ThrottlingRateLimit"]
    )


def test_failure_alarms_wired_to_a_topic(template):
    resources = template["Resources"]

    alarms = [
        (logical_id, resource["Properties"])
        for logical_id, resource in resources.items()
        if resource["Type"] == "AWS::CloudWatch::Alarm"
    ]

    alarm_names = {
        props["AlarmName"] for _, props in alarms
    }

    assert any(
        "documents-errors" in name for name in alarm_names
    )
    assert any(
        "aggregates-errors" in name for name in alarm_names
    )
    assert any("api-5xx" in name for name in alarm_names)

    # Every alarm routes into the topic.
    for _, props in alarms:
        assert props["AlarmActions"] == ["AlarmsTopic"]

    # The topic takes an optional email subscriber; with none the
    # subscription list is a no-value conditional.
    assert (
        template["Parameters"]["AlarmEmail"]["Default"] == ""
    )


def test_aggregates_alarm_tracks_the_function_dimension(template):
    """The aggregates alarm tracks the FUNCTION the schedule
    invokes, not the rule."""

    alarm = template["Resources"][
        "AggregatesFunctionErrorsAlarm"
    ]["Properties"]

    dimensions = {
        dimension["Name"]: dimension["Value"]
        for dimension in alarm["Dimensions"]
    }

    assert dimensions["FunctionName"] == "AggregatesFunction"

# ---------------------------------------------------------------
# This hardening pass: CORS, RBAC, hosting, upload gating
# ---------------------------------------------------------------

def get_resource(template, name):
    return template["Resources"][name]


def test_api_cors_is_pinned_to_the_frontend_origin(template):
    cors = (
        get_resource(template, "DocumentsHttpApi")["Properties"]
        ["CorsConfiguration"]
    )

    # parse_template neutralizes !-tags, so a !Ref arrives as its
    # bare logical name.
    assert cors["AllowOrigins"] == ["FrontendOrigin"]
    # Authorization + Content-Type, not a wildcard header grab.
    assert "Authorization" in cors["AllowHeaders"]
    assert "Content-Type" in cors["AllowHeaders"]
    assert "*" not in cors["AllowHeaders"]


def test_bucket_cors_pinned_too(template):
    rules = (
        get_resource(template, "DocumentsBucket")["Properties"]
        ["CorsConfiguration"]["CorsRules"]
    )

    assert all(
        rule["AllowedOrigins"] == ["FrontendOrigin"]
        for rule in rules
    )


def test_signup_is_admin_created_only(template):
    pool = get_resource(template, "UserPool")["Properties"]

    assert pool["AdminCreateUserConfig"]["AllowAdminCreateUserOnly"] is True


def test_rbac_groups_exist(template):
    approvers = get_resource(template, "ApproversGroup")["Properties"]
    users = get_resource(template, "UsersGroup")["Properties"]

    assert approvers["GroupName"] == "Approvers"
    assert users["GroupName"] == "Users"
    # Both must belong to the SAME pool the API authorizes against.
    assert approvers["UserPoolId"] == "UserPool"
    assert users["UserPoolId"] == "UserPool"


def test_documents_function_can_flip_upload_status(template):
    policies = (
        get_resource(template, "DocumentsFunction")["Properties"]["Policies"]
    )

    # Collect every dynamodb action scoped to the DocumentsTable.
    dynamo_actions = set()

    for statement in policies:
        for entry in statement["Statement"]:
            resource = entry.get("Resource")
            if isinstance(resource, (str, list)) or (
                isinstance(resource, dict)
                and resource.get("Fn::GetAtt") == ["DocumentsTable", "Arn"]
            ):
                dynamo_actions.update(entry["Action"])

    assert "dynamodb:UpdateItem" in dynamo_actions


def test_confirm_upload_route_exists(template):
    events = (
        get_resource(template, "DocumentsFunction")["Properties"]["Events"]
    )

    route = events["DocumentConfirmUploadApi"]["Properties"]

    assert route["Path"] == "/documents/{document_id}/confirm-upload"
    assert route["Method"] == "POST"


def test_frontend_bucket_is_private_and_encrypted(template):
    props = get_resource(template, "FrontendBucket")["Properties"]

    block = props["PublicAccessBlockConfiguration"]

    assert all(block.values())
    assert props["BucketEncryption"]


def test_frontend_bucket_policy_scopes_to_this_distribution(template):
    statement = (
        get_resource(template, "FrontendBucketPolicy")["Properties"]
        ["PolicyDocument"]["Statement"][0]
    )

    assert statement["Principal"]["Service"] == "cloudfront.amazonaws.com"
    # The grant is scoped to THIS distribution's ARN (parse_template
    # returns the Sub's list form as-is).
    source_arn = statement["Condition"]["StringEquals"]["AWS:SourceArn"]
    assert source_arn[0].startswith("arn:aws:cloudfront::")
    assert "distribution/${DistributionId}" in source_arn[0]
    assert source_arn[1] == {
        "AccountId": "AWS::AccountId",
        "DistributionId": "FrontendDistribution",
    }


def test_cloudfront_serves_the_spa_with_fallbacks(template):
    config = (
        get_resource(template, "FrontendDistribution")["Properties"]
        ["DistributionConfig"]
    )

    assert config["DefaultRootObject"] == "index.html"
    assert config["DefaultCacheBehavior"]["ViewerProtocolPolicy"] == (
        "redirect-to-https"
    )

    # SPA deep links: 403/404 serve the app shell itself.
    fallbacks = {
        response["ErrorCode"]: response["ResponsePagePath"]
        for response in config["CustomErrorResponses"]
    }
    assert fallbacks == {403: "/index.html", 404: "/index.html"}

    # The origin rides Origin Access Control (no public bucket). OAC
    # ids belong on the Origin itself per the CloudFront schema
    # (nesting one in S3OriginConfig fails CFN early validation).
    origin = config["Origins"][0]
    assert origin["OriginAccessControlId"]
    assert config["DefaultCacheBehavior"]["TargetOriginId"] == origin["Id"]


def test_template_exposes_the_frontend_hostname(template):
    outputs = template["Outputs"]

    assert outputs["FrontendDistributionDomain"]["Value"] == (
        "FrontendDistribution.DomainName"
    )
    assert outputs["FrontendBucketName"]["Value"] == "FrontendBucket"


# ---------------------------------------------------------------
# On-call shift scheduling + PagerDuty integration
# ---------------------------------------------------------------

def get_shift_gsi(table, name):
    for index in table["GlobalSecondaryIndexes"]:
        if index["IndexName"] == name:
            return index
    return None


def test_oncall_tables_and_indexes_exist(template):
    resources = template["Resources"]

    shifts = get_resource(template, "OncallShiftsTable")["Properties"]
    assert shifts["BillingMode"] == "PAY_PER_REQUEST"

    role_index = get_shift_gsi(shifts, "role-start-index")
    engineer_index = get_shift_gsi(shifts, "engineer-start-index")

    for index in (role_index, engineer_index):
        assert index is not None, "shifts GSI missing"
        assert index["Projection"]["ProjectionType"] == "ALL"

    assert role_index["KeySchema"] == [
        {"AttributeName": "role", "KeyType": "HASH"},
        {"AttributeName": "start_at", "KeyType": "RANGE"},
    ]
    assert engineer_index["KeySchema"] == [
        {"AttributeName": "engineer_email", "KeyType": "HASH"},
        {"AttributeName": "start_at", "KeyType": "RANGE"},
    ]

    notifications = (
        get_resource(template, "OncallNotificationsTable")["Properties"]
    )
    assert notifications["BillingMode"] == "PAY_PER_REQUEST"

    time_index = get_shift_gsi(notifications, "time-index")
    alarm_index = get_shift_gsi(notifications, "alarm-index")

    for index in (time_index, alarm_index):
        assert index is not None, "notifications GSI missing"
        assert index["Projection"]["ProjectionType"] == "ALL"

    assert time_index["KeySchema"][0] == {
        "AttributeName": "ts_key", "KeyType": "HASH"
    }
    assert alarm_index["KeySchema"][0] == {
        "AttributeName": "alarm_name", "KeyType": "HASH"
    }


def test_oncall_function_routes_wired(template):
    events = (
        get_resource(template, "OnCallFunction")["Properties"]["Events"]
    )

    http_routes = {
        (event["Properties"]["Path"], event["Properties"]["Method"])
        for event in events.values()
        if event["Type"] == "HttpApi"
    }

    expected_routes = {
        ("/oncall/me", "GET"),
        ("/oncall/current", "GET"),
        ("/oncall/shifts", "GET"),
        ("/oncall/shifts", "POST"),
        ("/oncall/shifts/{shift_id}", "PUT"),
        ("/oncall/shifts/{shift_id}", "DELETE"),
        ("/oncall/sync", "POST"),
        ("/oncall/notifications", "GET"),
    }

    assert http_routes == expected_routes
    assert all(
        event["Properties"]["ApiId"] == "DocumentsHttpApi"
        for name, event in events.items()
        if name.startswith("OnCall") and name.endswith("Api")
    )

    event_types = [event["Type"] for event in events.values()]
    assert event_types.count("SNS") == 1
    assert event_types.count("Schedule") == 1

    sns_topic = next(
        event["Properties"]["Topic"]
        for event in events.values()
        if event["Type"] == "SNS"
    )
    assert sns_topic == "AlarmsTopic"

    for event in events.values():
        if event["Type"] == "Schedule":
            assert event["Properties"]["Schedule"] == "rate(1 day)"


def test_oncall_alarm_routes_into_the_alarms_topic(template):
    alarm = get_resource(template, "OnCallFunctionErrorsAlarm")["Properties"]

    assert alarm["AlarmActions"] == ["AlarmsTopic"]
    assert alarm["OKActions"] == ["AlarmsTopic"]

    dimensions = {
        dimension["Name"]: dimension["Value"]
        for dimension in alarm["Dimensions"]
    }
    assert dimensions["FunctionName"] == "OnCallFunction"


def test_pagerduty_secret_is_created_empty(template):
    """No GenerateSecret/SecretString: CloudFormation must never hold
    or echo a PagerDuty credential; the operator fills it post-deploy."""

    props = get_resource(template, "PagerDutySecret")["Properties"]

    assert "SecretString" not in props
    assert "GenerateSecretString" not in props
    assert props["Name"].startswith("${AWS::StackName}-")


def test_oncall_function_reads_the_secret_but_cannot_write_it(template):
    policies = (
        get_resource(template, "OnCallFunction")["Properties"]["Policies"]
    )

    secret_actions = set()
    secret_scoped = False

    for statement in policies:
        for entry in statement["Statement"]:
            resource = entry.get("Resource")
            # parse_template neutralizes !-tags, so the scalar form of
            # !GetAtt arrives as the string "PagerDutySecret.Arn".
            if str(resource).startswith("PagerDutySecret"):
                secret_actions.update(entry["Action"])
                secret_scoped = True

    assert secret_scoped, "no policy scoped to the PagerDuty secret"

    # Read-only: the runtime role must never be able to mint or
    # overwrite the credential.
    assert "secretsmanager:GetSecretValue" in secret_actions
    assert "secretsmanager:DescribeSecret" in secret_actions
    assert not (
        {"secretsmanager:PutSecretValue", "secretsmanager:CreateSecret"}
        & secret_actions
    )


def test_pagerduty_schedule_id_defaults_to_empty(template):
    parameter = template["Parameters"]["PagerDutyScheduleId"]

    # Empty on purpose: without a schedule every sync is an audited
    # skip instead of a hard dependency on PagerDuty at deploy time.
    assert parameter["Default"] == ""

    env = (
        get_resource(template, "OnCallFunction")["Properties"]
        ["Environment"]["Variables"]
    )

    assert env["PD_SCHEDULE_ID"] == "PagerDutyScheduleId"
    assert env["PD_SECRET_ARN"] == "PagerDutySecret"
    assert env["SHIFTS_TABLE"] == "OncallShiftsTable"
    assert env["NOTIFICATIONS_TABLE"] == "OncallNotificationsTable"


def test_oncall_outputs_exposed(template):
    outputs = template["Outputs"]

    assert outputs["OncallShiftsTableName"]["Value"] == "OncallShiftsTable"
    assert outputs["OncallNotificationsTableName"]["Value"] == (
        "OncallNotificationsTable"
    )
    assert outputs["PagerDutySecretArn"]["Value"] == "PagerDutySecret"
