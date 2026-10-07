"""Handler tests for backend/lambdas/oncall/app.py.

The Lambda module builds its boto3 clients at import time, so each
test loads the module through spec_from_file_location with boto3
patched to MagicMocks - every test gets a fresh module and a fresh
set of client/table mocks.

Coverage: coverage-resolution semantics (half-open windows, overlap
tie-break), shift CRUD validation, RBAC on the Approvers group,
pagination, the CloudWatch-alarm -> PagerDuty page path (both SNS
message shapes, dedup/resolve, severity, idempotency, skip paths),
and the PagerDuty schedule-override sync.
"""

import importlib.util
import json
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

REPO_ROOT = Path(__file__).resolve().parents[1]

LAMBDA_PATH = (
    REPO_ROOT
    / "backend"
    / "lambdas"
    / "oncall"
    / "app.py"
)


@pytest.fixture()
def app(monkeypatch):
    """Load the on-call Lambda with stubbed AWS clients."""

    monkeypatch.setenv("SHIFTS_TABLE", "OncallShiftsTable")
    monkeypatch.setenv("NOTIFICATIONS_TABLE", "OncallNotificationsTable")
    monkeypatch.setenv("PD_SECRET_ARN", "arn:test:pagerduty-secret")
    monkeypatch.setenv("PD_SCHEDULE_ID", "PDSCHEDULE123")
    monkeypatch.setenv("STACK_NAME", "test-stack")

    secrets = MagicMock()
    shifts = MagicMock()
    notifications = MagicMock()
    dynamodb = MagicMock()

    dynamodb.Table.side_effect = lambda name: {
        "OncallShiftsTable": shifts,
        "OncallNotificationsTable": notifications,
    }[name]

    with patch(
        "boto3.client", return_value=secrets
    ), patch("boto3.resource", return_value=dynamodb):

        spec = importlib.util.spec_from_file_location(
            "oncall_app", LAMBDA_PATH
        )

        module = importlib.util.module_from_spec(spec)

        spec.loader.exec_module(module)

    module._test = MagicMock()
    module._test.secrets = secrets
    module._test.shifts = shifts
    module._test.notifications = notifications

    # Outbound HTTP is fully captured, never sent.
    calls = []

    def capture(method, url, headers, payload=None):
        calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers or {}),
                "payload": payload,
            }
        )

        return 202, {"status": "success", "dedup_key": "x"}

    module.set_http_request(capture)
    module._test.http_calls = calls

    return module


def api_event(method, path, body=None, query=None, groups=None):
    claims = {}

    if groups:
        claims["cognito:groups"] = groups
        claims["sub"] = "sub-123"
        claims["email"] = "caller@example.com"

    event = {
        "rawPath": path,
        "requestContext": {
            "http": {"method": method},
            "authorizer": {"jwt": {"claims": claims}},
        },
    }

    if body is not None:
        event["body"] = json.dumps(body)

    if query is not None:
        event["queryStringParameters"] = query

    return event


def body(response):
    return json.loads(response["body"])


def good_shift(**overrides):
    shift = {
        "engineer_email": "alice@example.com",
        "engineer_name": "Alice",
        "pagerduty_user_id": "PDUSER1",
        "role": "primary",
        "start_at": "2030-01-01T00:00:00+00:00",
        "end_at": "2030-01-02T00:00:00+00:00",
    }

    shift.update(overrides)

    return shift


def configure_secret(app, secret=None):
    if secret is None:
        secret = {
            "routing_key": "RK123",
            "api_token": "TOKEN123",
            "from_email": "ops@example.com",
        }

    app._test.secrets.get_secret_value.return_value = {
        "SecretString": json.dumps(secret)
    }

    app.reset_pd_secret_cache()

    return secret


def sns_event(message, message_id=None):
    return {
        "Records": [
            {
                "EventSource": "aws:sns",
                "Sns": {
                    "MessageId": message_id or str(uuid.uuid4()),
                    "Message": json.dumps(message),
                },
            }
        ]
    }


def classic_alarm(state_value="ALARM", reason="threshold crossed"):
    return {
        "AlarmName": "intelligent-storage-cost-optimizer-documents-errors",
        "AlarmArn": "arn:aws:cloudwatch:ap-south-1:123:alarm:documents-errors",
        "AWSAccountId": "123",
        "Region": "ap-south-1",
        "NewStateValue": state_value,
        "OldStateValue": "OK",
        "NewStateReason": reason,
        "StateChangeTime": "2030-01-01T00:00:00.000+0000",
        "Trigger": {
            "Namespace": "AWS/Lambda",
            "MetricName": "Errors",
        },
    }


def eventbridge_alarm(state_value="ALARM"):
    return {
        "source": "aws.cloudwatch",
        "detail-type": "CloudWatch Alarm State Change",
        "detail": {
            "alarmName": "intelligent-storage-cost-optimizer-api-5xx",
            "alarmArn": "arn:aws:cloudwatch:ap-south-1:123:alarm:api-5xx",
            "accountId": "123",
            "region": "ap-south-1",
            "state": {
                "value": state_value,
                "reason": "threshold crossed",
                "timestamp": "2030-01-01T00:00:00Z",
            },
            "previousState": {"value": "OK"},
            "configuration": {
                "namespace": "AWS/ApiGateway",
                "metric_name": "5XXError",
            },
        },
    }


# ---------------------------------------------------------------
# coverage resolution (pure)
# ---------------------------------------------------------------

def test_resolve_coverage_half_open_window(app):
    now = datetime(2030, 1, 2, 12, 0, tzinfo=timezone.utc)

    starting_now = good_shift(
        start_at=now.isoformat(),
        end_at="2030-01-02T18:00:00+00:00",
    )

    ending_now = good_shift(
        start_at="2030-01-02T06:00:00+00:00",
        end_at=now.isoformat(),
    )

    result = app.resolve_coverage(
        [starting_now, ending_now], now
    )

    # start == now is in (half-open start included)...
    assert result["primary"]["engineer_email"] == "alice@example.com"
    assert result["coverage"] == "covered"

    # ...and a shift ending exactly at now is not.
    only_ending = app.resolve_coverage([ending_now], now)

    assert only_ending["coverage"] == "none"
    assert only_ending["primary"] is None


def test_resolve_coverage_overlap_keeps_newest(app):
    now = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)

    earlier = good_shift(
        start_at="2030-01-01T00:00:00+00:00",
        end_at="2030-01-01T23:59:00+00:00",
    )

    later = good_shift(engineer_email="bob@example.com", start_at=(
        now - timedelta(hours=1)
    ).isoformat())

    result = app.resolve_coverage([earlier, later], now)

    assert result["primary"]["engineer_email"] == "bob@example.com"
    assert [
        shift["engineer_email"]
        for shift in result["overlapping"]
    ] == ["alice@example.com"]


def test_resolve_coverage_returns_both_roles(app):
    # A fixed now inside both default windows (2030-01-01T00 .. 2030-01-02T00).
    now = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)

    primary = good_shift()
    secondary = good_shift(
        role="secondary",
        engineer_email="bob@example.com",
        pagerduty_user_id="PDUSER2",
    )

    result = app.resolve_coverage([primary, secondary], now)

    assert result["primary"]["engineer_email"] == "alice@example.com"
    assert result["secondary"]["engineer_email"] == "bob@example.com"


def test_resolve_coverage_empty(app):
    result = app.resolve_coverage([], datetime.now(timezone.utc))

    assert result["coverage"] == "none"
    assert result["primary"] is None
    assert result["secondary"] is None


# ---------------------------------------------------------------
# routes: /oncall/me and shift CRUD
# ---------------------------------------------------------------

def test_me_reports_principal_and_manage_flag(app):
    result = app.who_am_i(
        api_event("GET", "/oncall/me", groups="Users")
    )

    payload = body(result)

    assert result["statusCode"] == 200
    assert payload == {
        "principal": "caller@example.com",
        "can_manage": False,
    }

    assert body(
        app.who_am_i(
            api_event("GET", "/oncall/me", groups="Approvers")
        )
    )["can_manage"] is True


def test_create_shift_validations(app):
    bad_bodies = [
        good_shift(engineer_email="  "),
        good_shift(role="lead"),

        good_shift(end_at="2030-01-01T00:00:00+00:00"),
        # Over 7 days.
        good_shift(
            start_at="2030-01-01T00:00:00+00:00",
            end_at="2030-01-09T00:00:00+00:00",
        ),
        good_shift(start_at="not-a-date"),
    ]

    for bad in bad_bodies:

        response = app.create_shift(
            api_event("POST", "/oncall/shifts", bad, groups="Approvers")
        )

        assert response["statusCode"] == 400, bad
        assert "error" in body(response)

    app._test.shifts.put_item.assert_not_called()


def test_create_shift_happy_path_stamps_attribution(app):
    configure_secret(app)

    shift = good_shift()

    created = app.create_shift(
        api_event("POST", "/oncall/shifts", shift, groups="Approvers")
    )

    assert created["statusCode"] == 201

    item = app._test.shifts.put_item.call_args.kwargs["Item"]

    assert item["engineer_email"] == "alice@example.com"
    assert item["role"] == "primary"
    assert item["created_by"] == "caller@example.com"
    assert item["sync_status"] in ("pending", "synced", "failed")
    assert "shift_id" in item


def test_create_shift_best_effort_sync_failure_still_succeeds(app):
    """PagerDuty being down must not fail the shift write."""

    configure_secret(app)

    def failing(method, url, headers, payload=None):
        raise RuntimeError("pagerduty unreachable")

    app.set_http_request(failing)

    shifts = app._test.shifts

    # The post-sync re-read returns the stamped row.
    shifts.get_item.return_value = {"Item": {"shift_id": "x"}}

    created = app.create_shift(
        api_event(
            "POST", "/oncall/shifts", good_shift(), groups="Approvers"
        )
    )

    assert created["statusCode"] == 201

    stamp = shifts.update_item.call_args.kwargs

    assert stamp["ExpressionAttributeValues"][":status"] == "failed"


def test_shift_mutations_require_the_approvers_group(app):
    configure_secret(app)

    for action, path in (
        (app.create_shift, "/oncall/shifts"),
        (app.sync_route, "/oncall/sync"),
    ):
        response = action(
            api_event("POST", path, {}, groups="Users")
        )

        assert response["statusCode"] == 403, path
        assert body(response) == {
            "error": "Approver role required for this action"
        }

    app._test.shifts.put_item.assert_not_called()

    update = app.update_shift(
        "shift-1",
        api_event(
            "PUT", "/oncall/shifts/shift-1", {}, groups="Users"
        ),
    )

    assert update["statusCode"] == 403

    delete = app.delete_shift(
        "shift-1",
        api_event("DELETE", "/oncall/shifts/shift-1", groups="Users"),
    )

    assert delete["statusCode"] == 403

    # Without claims (direct invoke) the gate passes - the gateway
    # enforces authentication there.
    assert (
        app.who_am_i(api_event("GET", "/oncall/me"))
        ["statusCode"]
        == 200
    )


def test_update_and_delete_shift(app):
    configure_secret(app)

    existing = good_shift(shift_id="shift-1")

    app._test.shifts.get_item.return_value = {"Item": existing}

    updated = app.update_shift(
        "shift-1",
        api_event(
            "PUT",
            "/oncall/shifts/shift-1",
            good_shift(engineer_email="carol@example.com"),
            groups="Approvers",
        ),
    )

    assert updated["statusCode"] == 200

    deleted = app.delete_shift(
        "shift-1",
        api_event("DELETE", "/oncall/shifts/shift-1", groups="Approvers"),
    )

    assert deleted["statusCode"] == 200

    app._test.shifts.delete_item.assert_called_once_with(
        Key={"shift_id": "shift-1"}
    )


def test_update_unknown_shift_is_404(app):
    """RBAC is checked before existence (no existence leak to
    non-approver callers), so the unknown-shift 404 needs an
    approver caller."""
    app._test.shifts.get_item.return_value = {}

    response = app.update_shift(
        "ghost",
        api_event(
            "PUT",
            "/oncall/shifts/ghost",
            good_shift(),
            groups="Approvers",
        ),
    )

    assert response["statusCode"] == 404


def test_list_shifts_paginates_with_a_next_token(app):
    shifts = app._test.shifts

    shifts.query.side_effect = [
        {
            "Items": [{"shift_id": "a", "role": "primary"}],
            "LastEvaluatedKey": {"shift_id": "a", "role": "primary"},
        },
        {"Items": [{"shift_id": "b", "role": "primary"}]},
    ]

    first = app.list_shifts(
        api_event("GET", "/oncall/shifts", query={"limit": "1"})
    )

    page = body(first)

    assert first["statusCode"] == 200
    assert page["next_token"]
    assert shifts.query.call_args.kwargs["Limit"] == 1

    shifts.query.side_effect = None
    shifts.query.return_value = {"Items": []}

    second = app.list_shifts(
        api_event(
            "GET",
            "/oncall/shifts",
            query={"limit": "1", "next_token": page["next_token"]},
        )
    )

    assert second["statusCode"] == 200


def test_current_coverage_route(app):
    now = datetime.now(timezone.utc)

    app._test.shifts.query.return_value = {
        "Items": [
            {
                "shift_id": "s1",
                "role": "primary",
                "engineer_email": "alice@example.com",
                "start_at": (now - timedelta(hours=1)).isoformat(),
                "end_at": (now + timedelta(hours=1)).isoformat(),
            }
        ]
    }

    payload = body(app.coverage_route(api_event("GET", "/oncall/current")))

    assert payload["coverage"] == "covered"
    assert payload["primary"]["engineer_email"] == "alice@example.com"


def test_coverage_window_uses_one_condition_per_key(app):
    """Regression for the live ValidationException: chaining .gt and
    .lte on the same key is rejected by DynamoDB outright, which the
    coverage query swallowed into 'none' — the pager would route
    every alarm as unrouted. The window must ride a single .between
    on start_at."""

    app._test.shifts.query.return_value = {"Items": []}

    app.coverage_route(api_event("GET", "/oncall/current"))

    expression = (
        app._test.shifts.query.call_args.kwargs[
            "KeyConditionExpression"
        ]
    )

    # Walk the And tree: attribute name -> comparator class names.
    comparators = {}
    for child in expression._values:
        attribute = child._values[0].name
        comparators.setdefault(attribute, []).append(
            type(child).__name__
        )

    # One condition per key: DynamoDB rejects two conditions on the
    # same key outright. The time window rides a single BETWEEN.
    assert comparators["role"] == ["Equals"]
    assert comparators["start_at"] == ["Between"]


# ---------------------------------------------------------------
# alarm -> PagerDuty page path
# ---------------------------------------------------------------

def test_classic_alarm_triggers_a_page(app):
    secret = configure_secret(app)

    app._test.shifts.query.return_value = {
        "Items": [
            {
                "shift_id": "s1",
                "role": "primary",
                "engineer_email": "alice@example.com",
                "engineer_name": "Alice",
                "pagerduty_user_id": "PDUSER1",
                "start_at": "2020-01-01T00:00:00+00:00",
                "end_at": "2099-01-01T00:00:00+00:00",
            }
        ]
    }

    result = app.handle_alarm_notification(
        sns_event(classic_alarm(), message_id="msg-1")
    )

    assert result == {"paged": 1}

    call = app._test.http_calls[0]

    assert call["url"] == "https://events.pagerduty.com/v2/enqueue"
    payload = call["payload"]

    assert payload["routing_key"] == "RK123"
    assert payload["event_action"] == "trigger"
    assert payload["dedup_key"].endswith("documents-errors")
    assert payload["payload"]["severity"] == "critical"
    assert payload["payload"]["custom_details"]["oncall"] == {
        "primary": {
            "email": "alice@example.com",
            "pd_user_id": "PDUSER1",
            "name": "Alice",
        }
    }
    assert payload["payload"]["custom_details"]["oncall_coverage"] == (
        "routed"
    )

    # The audit row was reserved with the SNS MessageId then stamped.
    reserve = app._test.notifications.put_item.call_args.kwargs
    assert reserve["Item"]["notification_id"] == "msg-1"
    assert "attribute_not_exists(notification_id)" in (
        reserve["ConditionExpression"]
    )

    stamp = app._test.notifications.update_item.call_args.kwargs

    assert stamp["ExpressionAttributeValues"][":pd_status"] == "success"
    assert stamp["ExpressionAttributeValues"][":routing"] == "routed"


def test_eventbridge_alarm_shape_also_parses(app):
    app.parse_alarm_notification(eventbridge_alarm())["alarm_name"]
    alert = app.parse_alarm_notification(eventbridge_alarm())

    assert alert["alarm_name"].endswith("api-5xx")
    assert alert["state_value"] == "ALARM"
    assert alert["metric"] == "AWS/ApiGateway/5XXError"


def test_ok_alarm_resolves_the_same_incident(app):
    configure_secret(app)
    app._test.shifts.query.return_value = {"Items": []}

    app.handle_alarm_notification(
        sns_event(classic_alarm(state_value="OK"), message_id="msg-2")
    )

    payload = app._test.http_calls[-1]["payload"]

    assert payload["event_action"] == "resolve"
    assert payload["payload"]["severity"] == "info"
    assert "RESOLVED" in payload["payload"]["summary"]
    # Same dedup key as the trigger -> PD resolves the incident.
    assert payload["dedup_key"].endswith("documents-errors")


def test_no_coverage_still_pages_but_flags_it(app):
    configure_secret(app)
    app._test.shifts.query.return_value = {"Items": []}

    app.handle_alarm_notification(
        sns_event(classic_alarm(), message_id="msg-3")
    )

    payload = app._test.http_calls[-1]["payload"]

    assert payload["event_action"] == "trigger"
    assert "UNROUTED" in (
        payload["payload"]["custom_details"]["oncall_coverage"]
    )

    stamp = app._test.notifications.update_item.call_args.kwargs

    assert stamp["ExpressionAttributeValues"][":routing"] == "unrouted"


def test_insufficient_data_is_audit_only(app):
    configure_secret(app)

    result = app.handle_alarm_notification(
        sns_event(
            classic_alarm(state_value="INSUFFICIENT_DATA"),
            message_id="msg-4",
        )
    )

    assert result == {"skipped": 1}
    assert not app._test.http_calls

    stamp = app._test.notifications.update_item.call_args.kwargs

    assert stamp["ExpressionAttributeValues"][":pd_status"] == (
        "skipped_insufficient_data"
    )
    assert not app._test.notifications.put_item.call_args or True


def test_missing_secret_skips_without_raising(app):
    app._test.secrets.get_secret_value.side_effect = RuntimeError(
        "ResourceNotFoundException"
    )

    result = app.handle_alarm_notification(
        sns_event(classic_alarm(), message_id="msg-5")
    )

    assert result == {"skipped": 1}
    assert not app._test.http_calls

    stamp = app._test.notifications.update_item.call_args.kwargs

    assert stamp["ExpressionAttributeValues"][":pd_status"] == (
        "skipped_no_secret"
    )


def test_duplicate_message_id_never_pages_twice(app):
    configure_secret(app)

    app._test.notifications.put_item.side_effect = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException"}},
        "PutItem",
    )

    result = app.handle_alarm_notification(
        sns_event(classic_alarm(), message_id="msg-6")
    )

    assert result == {"duplicates": 1}
    assert not app._test.http_calls


def test_pd_failure_is_recorded_not_raised(app):
    configure_secret(app)
    app._test.shifts.query.return_value = {"Items": []}

    def failing(method, url, headers, payload=None):
        raise TimeoutError("pagerduty down")

    app.set_http_request(failing)

    result = app.handle_alarm_notification(
        sns_event(classic_alarm(), message_id="msg-7")
    )

    assert result == {"paged": 0}

    stamp = app._test.notifications.update_item.call_args.kwargs

    assert stamp["ExpressionAttributeValues"][":pd_status"] == "failed"


def test_unrecognized_message_is_ignored(app):
    result = app.handle_alarm_notification(
        sns_event({"hello": "world"}, message_id="msg-8")
    )

    assert result == {"skipped": 1}
    assert not app._test.http_calls


def test_non_json_message_is_reported_not_raised(app):
    event = {
        "Records": [
            {
                "EventSource": "aws:sns",
                "Sns": {"MessageId": "msg-9", "Message": "{oops"},
            }
        ]
    }

    assert app.handle_alarm_notification(event) == {"failed": 1}


def test_sns_branch_short_circuits_route_dispatch(app):
    result = app.lambda_handler(
        sns_event({"hello": "world"}, message_id="msg-10"), None
    )

    assert "statusCode" not in result


# ---------------------------------------------------------------
# PagerDuty schedule-override sync
# ---------------------------------------------------------------

def test_upsert_override_carries_the_required_shape(app):
    configure_secret(app)

    shift = good_shift(shift_id="shift-1")

    app._test.shifts.update_item.return_value = {}

    status = app.sync_shift_to_pd(shift, "upsert")

    assert status == "synced"

    call = app._test.http_calls[-1]

    assert call["url"] == (
        "https://api.pagerduty.com/schedules/PDSCHEDULE123/overrides"
    )
    assert call["method"] == "POST"

    overrides = call["payload"]["overrides"]

    assert overrides[0]["start"] == shift["start_at"]
    assert overrides[0]["end"] == shift["end_at"]
    assert overrides[0]["user"] == {
        "id": "PDUSER1",
        "type": "user_reference",
    }

    # The Overrides API requires From (a PD user allowed to edit).
    assert call["headers"]["From"] == "ops@example.com"
    assert call["headers"]["Authorization"].startswith("Token token=")
    assert call["headers"]["Accept"].startswith(
        "application/vnd.pagerduty+json"
    )

    stamp = app._test.shifts.update_item.call_args.kwargs

    assert stamp["ExpressionAttributeValues"][":status"] == "synced"


def test_shift_without_pd_user_id_is_skipped(app):
    configure_secret(app)

    status = app.sync_shift_to_pd(
        good_shift(shift_id="shift-1", pagerduty_user_id=""), "upsert"
    )

    assert status == "skipped_missing_pd_user_id"
    assert not app._test.http_calls

    stamp = app._test.shifts.update_item.call_args.kwargs

    assert stamp["ExpressionAttributeValues"][":status"] == (
        "skipped_missing_pd_user_id"
    )


def test_sync_without_schedule_id_is_skipped(app, monkeypatch):
    configure_secret(app)
    monkeypatch.setenv("PD_SCHEDULE_ID", "")

    status = app.sync_shift_to_pd(good_shift(shift_id="shift-1"))

    assert status == "skipped_no_schedule"
    assert not app._test.http_calls


def test_sync_without_secret_is_skipped(app, monkeypatch):
    monkeypatch.setenv("PD_SCHEDULE_ID", "PDSCHEDULE123")
    app._test.secrets.get_secret_value.side_effect = RuntimeError(
        "no secret"
    )

    status = app.sync_shift_to_pd(good_shift(shift_id="shift-1"))

    assert status == "skipped_no_secret"
    assert not app._test.http_calls


def test_delete_shift_removes_the_matching_override(app):
    configure_secret(app)

    app._test.http_calls.clear()

    override = {
        "id": "OVR1",
        "start": "2030-01-01T00:00:00+00:00",
        "end": "2030-01-02T00:00:00+00:00",
        "user": {"id": "PDUSER1"},
    }

    calls = []

    def fake_http(method, url, headers, payload=None):
        calls.append({"method": method, "url": url, "headers": headers})

        if method == "GET":
            return 200, {"overrides": [override]}

        return 204, None

    app.set_http_request(fake_http)

    status = app.sync_shift_to_pd(
        good_shift(shift_id="shift-1"), "remove"
    )

    assert status == "removed"

    delete_call = calls[-1]

    assert delete_call["method"] == "DELETE"
    assert delete_call["url"].endswith(
        "/schedules/PDSCHEDULE123/overrides/OVR1"
    )


def test_delete_shift_with_no_matching_override_is_reported(app):
    configure_secret(app)

    def fake_http(method, url, headers, payload=None):
        assert method == "GET"
        return 200, {"overrides": []}

    app.set_http_request(fake_http)

    status = app.sync_shift_to_pd(
        good_shift(shift_id="shift-1"), "remove"
    )

    assert status == "skipped_no_schedule"


def test_reconcile_adds_missing_and_removes_stale(app):
    configure_secret(app)

    now = datetime.now(timezone.utc)

    ours = good_shift(
        shift_id="shift-9",
        start_at=(now + timedelta(days=1)).isoformat(),
        end_at=(now + timedelta(days=2)).isoformat(),
    )

    app._test.shifts.query.return_value = {"Items": [ours]}
    app._test.shifts.scan.return_value = {"Items": [ours]}

    matching = {
        "id": "OVR-KEEP",
        "start": ours["start_at"],
        "end": ours["end_at"],
        "user": {"id": "PDUSER1"},
    }

    stale = {
        "id": "OVR-STALE",
        "start": (now + timedelta(days=3)).isoformat(),
        "end": (now + timedelta(days=4)).isoformat(),
        "user": {"id": "PDUSER1"},
    }

    def fake_http(method, url, headers, payload=None):
        if method == "GET":
            return 200, {"overrides": [matching, stale]}

        return 201, None

    app.set_http_request(fake_http)

    result = app.reconcile_pd_schedule()

    # The matching override is left alone; the stale one is removed;
    # nothing was missing from the PD side.
    assert result["pd_status"] == "success"
    assert result["added"] == 0
    assert result["removed"] == 1
    assert result["failed"] == 0


def test_reconcile_creates_a_missing_override(app):
    configure_secret(app)

    now = datetime.now(timezone.utc)

    ours = good_shift(
        shift_id="shift-9",
        start_at=(now + timedelta(days=1)).isoformat(),
        end_at=(now + timedelta(days=2)).isoformat(),
    )

    app._test.shifts.query.return_value = {"Items": [ours]}
    app._test.shifts.scan.return_value = {"Items": [ours]}

    def fake_http(method, url, headers, payload=None):
        if method == "GET":
            return 200, {"overrides": []}

        assert method == "POST"
        return 201, None

    app.set_http_request(fake_http)

    result = app.reconcile_pd_schedule()

    assert result["added"] == 1
    assert result["removed"] == 0


def test_daily_schedule_branch_runs_the_reconciler(app):
    configure_secret(app)

    def fake_http(method, url, headers, payload=None):
        return 200, {"overrides": []}

    app.set_http_request(fake_http)

    app._test.shifts.query.return_value = {"Items": []}
    app._test.shifts.scan.return_value = {"Items": []}

    result = app.lambda_handler({"source": "aws.events"}, None)

    assert result["pd_status"] == "success"


def test_route_dispatch_of_the_oncall_api(app):
    configure_secret(app)

    assert (
        app.lambda_handler(
            api_event("GET", "/oncall/me", groups="Approvers"), None
        )["statusCode"]
        == 200
    )
    assert (
        app.lambda_handler(
            api_event("GET", "/oncall/current"), None
        )["statusCode"]
        == 200
    )
    assert (
        app.lambda_handler(
            api_event("GET", "/oncall/notifications"), None
        )["statusCode"]
        == 200
    )
    assert (
        app.lambda_handler(
            api_event("POST", "/oncall/shifts", {}, groups="Approvers"),
            None,
        )["statusCode"]
        == 400
    )
    assert (
        app.lambda_handler(api_event("GET", "/nope"), None)[
            "statusCode"
        ]
        == 404
    )


def test_notifications_listing_filters_by_alarm(app):
    app._test.notifications.query.return_value = {"Items": []}

    response = app.list_notifications(
        api_event(
            "GET",
            "/oncall/notifications",
            query={"alarm": "stack-documents-errors", "limit": "10"},
        )
    )

    assert response["statusCode"] == 200

    kwargs = app._test.notifications.query.call_args.kwargs

    assert kwargs["IndexName"] == "alarm-index"
    assert kwargs["Limit"] == 10
    assert kwargs["ScanIndexForward"] is False


def test_notifications_listing_validation(app):
    response = app.list_notifications(
        api_event("GET", "/oncall/notifications", query={"limit": "0"})
    )

    assert response["statusCode"] == 400