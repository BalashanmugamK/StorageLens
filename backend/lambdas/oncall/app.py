"""On-call shift scheduling + PagerDuty integration (StorageLens).

Owns three jobs, all in one Lambda:

1.  Shift CRUD + coverage (the HTTP API surface). Admins (the
    Approvers group) create shift windows for engineers; any signed-in
    user can read who covers right now. Shift windows are half-open
    UTC intervals in a dedicated DynamoDB table, so "who is on call"
    is a bounded query, never a scan of history.

2.  Alarm paging. The stack's CloudWatch alarms fan into AlarmsTopic;
    an SNS subscription invokes this Lambda, which pages PagerDuty
    through the Events API v2. Triggered incidents use the alarm ARN
    as the dedup key, so the matching OK notification resolves the
    same incident. Whoever covers right now is resolved before the
    page and stamped on both the PD payload and the audit row.

3.  Two-way schedule sync. Shifts are the source of truth; each
    mutation is pushed into PagerDuty as a schedule override (REST
    v2), and a daily EventBridge run reconciles the next 30 days so
    drift converges. PD credentials (routing key, API token, From
    e-mail) live in one Secrets Manager secret, created empty by the
    template and filled by the admin post-deploy; every missing
    credential / schedule id degrades to an auditable SKIPPED, never
    a raised error.

Every page attempt - successful, skipped, or failed - is recorded in
the notifications audit table (keyed by the SNS MessageId, giving
at-least-once idempotency). Acknowledgment and escalation after a
page are deliberately PagerDuty's escalation policy, not ours.
"""

import base64
import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib import error as urllib_error, request as urllib_request

import boto3
from boto3.dynamodb.conditions import Attr, Key

logger = logging.getLogger(__name__)

# Region comes from the Lambda runtime's AWS_REGION environment
# variable (resolved by boto3), so the deployment works in any
# region without a hard-coded regional endpoint.
secretsmanager = boto3.client("secretsmanager")

dynamodb = boto3.resource("dynamodb")

SHIFTS_TABLE = os.environ.get("SHIFTS_TABLE")
NOTIFICATIONS_TABLE = os.environ.get("NOTIFICATIONS_TABLE")

table = (
    dynamodb.Table(SHIFTS_TABLE)
    if SHIFTS_TABLE
    else None
)

notifications_table = (
    dynamodb.Table(NOTIFICATIONS_TABLE)
    if NOTIFICATIONS_TABLE
    else None
)

APPROVER_GROUP = "Approvers"

ROLE_PRIMARY = "primary"
ROLE_SECONDARY = "secondary"
SHIFT_ROLES = (ROLE_PRIMARY, ROLE_SECONDARY)

# A single shift covers at most 7 days. The bound does two things:
# stops an accidental multi-month window from locking the rotation,
# and bounds the coverage query (cutoff = now - MAX_SHIFT_HOURS) so
# historical rows never inflate the read.
MAX_SHIFT_HOURS = 168

DEFAULT_PAGE_LIMIT = 25

PD_EVENTS_API_URL = "https://events.pagerduty.com/v2/enqueue"
PD_REST_API_URL = "https://api.pagerduty.com"

PD_USER_REFERENCE = "user_reference"

# The PagerDuty credentials secret holds exactly this JSON document:
# {"routing_key": "<Events API v2 integration key>",
#  "api_token": "<REST v2 token>",
#  "from_email": "<address of a PD user that can edit the schedule>"}
PD_SECRET_KEYS = ("routing_key", "api_token", "from_email")

# Deliberate single seam over outbound HTTP so tests can capture both
# the PagerDuty Events API and the REST overrides API.
_http_request = None

# Cold-start cache of the PD secret; the container lifetime is the
# cache. A None here is sticky until a new container only because a
# missing secret does not fix itself mid-flight, but we re-read on
# every miss so a fill shortly after deploy is picked up.
_pd_secret_cache = None


def response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json"
        },
        "body": json.dumps(
            body,
            default=lambda value: int(value)
            if isinstance(value, Decimal)
            else str(value)
        )
    }


# ---- identity / RBAC -------------------------------------------------

def actor_from_event(event):
    """Identity of the signed-in caller as recorded by the JWT
    authorizer - stamped on shift mutations. Claims are absent
    outside the API (tests/direct invokes), which is fine."""
    claims = (
        event.get("requestContext", {})
        .get("authorizer", {})
        .get("jwt", {})
        .get("claims", {})
    )

    email = claims.get("email")
    subject = claims.get("sub")

    return email if email else subject


def is_approver(event):
    """True when the caller's JWT carries the Approvers group.

    HTTP API's JWT authorizer surfaces `cognito:groups` in
    requestContext.authorizer.jwt.claims. For a single group Cognito
    sends a plain string; multiple groups arrive comma-joined (a raw
    list is handled for robustness across API GW versions). Claims
    are absent outside the API (direct invokes/tests), where the
    check passes - authorization is the gateway's job there."""
    claims = (
        event.get("requestContext", {})
        .get("authorizer", {})
        .get("jwt", {})
        .get("claims", {})
    )

    raw = claims.get("cognito:groups")

    if not raw:
        return True

    if isinstance(raw, str):
        groups = [g.strip() for g in raw.split(",") if g.strip()]
    elif isinstance(raw, (list, tuple)):
        groups = [str(g).strip() for g in raw if str(g).strip()]
    else:
        groups = []

    return APPROVER_GROUP in groups


def principal_from_event(event):
    claims = (
        event.get("requestContext", {})
        .get("authorizer", {})
        .get("jwt", {})
        .get("claims", {})
    )

    return claims.get("email") or claims.get("sub") or claims.get("username")


# ---- pagination -------------------------------------------------------

def parse_page_params(query):
    """Decode the optional `limit` (1-1000) and `next_token` (URL-safe
    base64 of a JSON LastEvaluatedKey) shared by the listing routes.
    Returns (limit, start_key); limit is None when unset, which keeps
    the collect-all behavior. Raises ValueError on a malformed
    request."""
    limit_raw = (query or {}).get("limit")

    if limit_raw in (None, ""):
        limit = None
    else:
        limit = int(limit_raw)

        if not (1 <= limit <= 1000):
            raise ValueError("limit must be between 1 and 1000")

    token = (query or {}).get("next_token")

    if token in (None, ""):
        return limit, None

    start_key = json.loads(
        base64.urlsafe_b64decode(token.encode()).decode()
    )

    if not isinstance(start_key, dict):
        raise ValueError("next_token is malformed")

    return limit, start_key


def encode_page_token(last_evaluated_key):
    return base64.urlsafe_b64encode(
        json.dumps(last_evaluated_key).encode()
    ).decode()


# ---- secrets ----------------------------------------------------------

def _pd_secret():
    """PagerDuty credentials from Secrets Manager, or None when the
    secret is missing/empty/malformed - every caller degrades to a
    SKIPPED audit row rather than raising."""
    global _pd_secret_cache

    if _pd_secret_cache is not None:
        return _pd_secret_cache

    try:
        result = secretsmanager.get_secret_value(
            SecretId=os.environ["PD_SECRET_ARN"]
        )

        secret = json.loads(result.get("SecretString") or "{}")
    except Exception:
        logger.exception("could not load the PagerDuty secret")
        return None

    if not secret or not all(key in secret for key in ("routing_key",)):
        logger.warning("PagerDuty secret is present but incomplete")
        return None

    _pd_secret_cache = secret

    return secret


def reset_pd_secret_cache():
    """Test hook: clear the cached secret between fixtures."""
    global _pd_secret_cache
    _pd_secret_cache = None


# ---- pure helpers -----------------------------------------------------

def resolve_coverage(shifts, now):
    """Who covers `now`, given shift rows whose start_at/end_at are
    ISO-8601 UTC. Pure so both the UI route and the pager path share
    exactly one definition.

    - a shift covers now iff start_at <= now < end_at (half-open);
    - per role, the newest covering start_at wins; any additional
      covering shift for the role is carried in `overlapping`
      (a UI warning, not an error - admins are expected not to stack
      shifts, and the audit log keeps the winner stamped).
    """
    primary = None
    secondary = None
    overlapping = []

    for shift in shifts:
        start = _parse_iso(shift.get("start_at"))
        end = _parse_iso(shift.get("end_at"))

        if start is None or end is None or not (start <= now < end):
            continue

        role = shift.get("role")

        if role == ROLE_PRIMARY:
            current = primary
        elif role == ROLE_SECONDARY:
            current = secondary
        else:
            continue

        if current is None:
            if role == ROLE_PRIMARY:
                primary = shift
            else:
                secondary = shift
        else:
            current_start = _parse_iso(current["start_at"])

            if start > current_start:
                overlapping.append(current)

                if role == ROLE_PRIMARY:
                    primary = shift
                else:
                    secondary = shift
            else:
                overlapping.append(shift)

    coverage = (
        "covered" if (primary or secondary) else "none"
    )

    return {
        "now": now.isoformat(),
        "primary": primary,
        "secondary": secondary,
        "overlapping": overlapping,
        "coverage": coverage,
    }


def _parse_iso(value):
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed


def validate_shift_input(body):
    """Field-level validation for create/update. Returns
    (errors, cleaned) - errors is a list of human-readable strings."""
    errors = []

    engineer_email = str(body.get("engineer_email") or "").strip()

    if not engineer_email:
        errors.append("engineer_email must be a non-empty string")

    role = str(body.get("role") or "").strip().lower()

    if role not in SHIFT_ROLES:
        errors.append(
            f"role must be one of {sorted(SHIFT_ROLES)}"
        )

    start_at = _parse_iso(body.get("start_at"))
    end_at = _parse_iso(body.get("end_at"))

    if start_at is None:
        errors.append("start_at must be an ISO-8601 datetime")
    if end_at is None:
        errors.append("end_at must be an ISO-8601 datetime")

    if start_at is not None and end_at is not None:
        if not (start_at < end_at):
            errors.append("start_at must be before end_at")
        elif (end_at - start_at) > timedelta(hours=MAX_SHIFT_HOURS):
            errors.append(
                f"a shift may span at most {MAX_SHIFT_HOURS} hours"
            )

    cleaned = {
        "engineer_email": engineer_email,
        "engineer_name": str(body.get("engineer_name") or "").strip(),
        "pagerduty_user_id": str(
            body.get("pagerduty_user_id") or ""
        ).strip(),
        "role": role,
        "start_at": start_at.isoformat() if start_at else None,
        "end_at": end_at.isoformat() if end_at else None,
    }

    return errors, cleaned


def pd_severity_for_state(state_value):
    """CloudWatch alarm state -> PagerDuty severity (ALARM pages
    critical; OK is carried on the resolve event as info)."""
    return {
        "ALARM": "critical",
        "OK": "info",
        "INSUFFICIENT_DATA": "warning",
    }.get(state_value, "error")


# ---- PagerDuty credentials / HTTP -------------------------------------

def _pd_headers(secret):
    return {
        "Authorization": f"Token token={secret.get('api_token', '')}",
        "Accept": "application/vnd.pagerduty+json;version=2",
        "Content-Type": "application/json",
        "From": secret.get("from_email", ""),
    }


def _default_http_request(method, url, headers, payload):
    body = json.dumps(payload).encode() if payload is not None else None

    request = urllib_request.Request(
        url, data=body, headers=headers, method=method
    )

    try:
        with urllib_request.urlopen(request, timeout=10) as response:
            raw = response.read().decode("utf-8", errors="replace")
            status = response.status
    except urllib_error.HTTPError as http_error:
        raw = http_error.read().decode("utf-8", errors="replace")
        status = http_error.code
    except Exception:
        raise

    try:
        return status, json.loads(raw)
    except (TypeError, ValueError):
        return status, raw


def http(method, url, headers, payload=None):
    global _http_request

    if _http_request is None:
        _http_request = _default_http_request

    return _http_request(method, url, headers or {}, payload)


def set_http_request(implementation):
    """Test seam: replace the outbound HTTP entry point entirely."""
    global _http_request
    _http_request = implementation


# ---- alarm notifications ----------------------------------------------

def parse_alarm_notification(message):
    """CloudWatch posts alarms to SNS in the classic shape
    (AlarmName/NewStateValue at the top level); an EventBridge relay
    would carry them nested under detail. Both shapes are handled so
    the pager keeps working however alarms reach the topic."""
    if not isinstance(message, dict):
        return None

    if message.get("AlarmName"):
        trigger = message.get("Trigger") or {}

        return {
            "alarm_name": message["AlarmName"],
            "alarm_arn": message.get("AlarmArn", ""),
            "state_value": message.get("NewStateValue", ""),
            "old_state_value": message.get("OldStateValue", ""),
            "state_reason": message.get("NewStateReason", ""),
            "state_change_time": message.get("StateChangeTime", ""),
            "account_id": message.get("AWSAccountId", ""),
            "region": message.get("Region", ""),
            "metric": f"{trigger.get('Namespace', '')}/"
            f"{trigger.get('MetricName', '')}",
        }

    detail = message.get("detail") or {}

    if detail.get("alarmName"):
        state = detail.get("state") or {}

        return {
            "alarm_name": detail["alarmName"],
            "alarm_arn": detail.get("alarmArn", ""),
            "state_value": state.get("value", ""),
            "old_state_value": (
                (detail.get("previousState") or {}).get("value", "")
            ),
            "state_reason": state.get("reason", ""),
            "state_change_time": state.get("timestamp", ""),
            "account_id": detail.get("accountId", ""),
            "region": detail.get("region", ""),
            "metric": _detail_metric(detail),
        }

    return None


def _detail_metric(detail):
    configuration = detail.get("configuration") or {}

    return f"{configuration.get('namespace', '')}/" f"{configuration.get('metric_name', '')}"


def build_pd_alert(alert, coverage, secret, stack_context):
    """The Events API v2 payload for one alarm state change.

    dedup_key is the alarm ARN: the ALARM event triggers the incident
    and the matching OK event resolves it - no mapping table, the
    lifecycle is exactly ALARM <-> OK. A gap in the shift table never
    silences an alarm: the page still goes out flagged UNROUTED so
    PagerDuty's own schedule/escalation policy is the human fallback.
    """
    state_value = alert["state_value"]
    resolving = state_value == "OK"

    oncall = {}
    if coverage.get("primary"):
        primary = coverage["primary"]
        oncall["primary"] = {
            "email": primary.get("engineer_email"),
            "pd_user_id": primary.get("pagerduty_user_id") or "",
            "name": primary.get("engineer_name") or "",
        }

    if coverage.get("secondary"):
        secondary = coverage["secondary"]
        oncall["secondary"] = {
            "email": secondary.get("engineer_email"),
            "pd_user_id": secondary.get("pagerduty_user_id") or "",
            "name": secondary.get("engineer_name") or "",
        }

    routed = bool(coverage.get("primary") or coverage.get("secondary"))

    state_change = (
        f"{alert.get('old_state_value') or '?'} -> {state_value}"
    )

    if resolving:
        summary = (
            f"[RESOLVED] {alert['alarm_name']} returned to OK"
        )
    else:
        summary = (
            f"[{pd_severity_for_state(state_value).upper()}] "
            f"{alert['alarm_name']}: {state_change}"
        )

    payload = {
        "routing_key": secret.get("routing_key"),
        "event_action": "resolve" if resolving else "trigger",
        "dedup_key": alert.get("alarm_arn") or alert["alarm_name"],
        "client": "StorageLens",
        "payload": {
            "summary": summary[:1024],
            "source": alert.get("alarm_arn") or alert["alarm_name"],
            "severity": "info" if resolving else pd_severity_for_state(
                state_value
            ),
            "custom_details": {
                "state_change": state_change,
                "state_reason": (alert.get("state_reason") or "")[:1024],
                "metric": alert.get("metric", ""),
                "account_id": alert.get("account_id", ""),
                "region": alert.get("region", ""),
                "stack": stack_context,
                "oncall": oncall,
                "oncall_coverage": (
                    "routed"
                    if routed
                    else "UNROUTED: no shift covers this time"
                ),
            },
        },
    }

    if alert.get("state_change_time"):
        payload["payload"]["timestamp"] = alert["state_change_time"]

    return payload, routed


def record_notification_start(notification_id):
    """Reserve the audit row for this SNS MessageId before any PD
    call. A redelivered message loses the conditional put and is
    reported as a duplicate - the page must fire at most once per
    delivery identity."""
    try:
        notifications_table.put_item(
            Item={
                "notification_id": notification_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "ts_key": "NOTIF",
                "pd_status": "processing",
            },
            ConditionExpression="attribute_not_exists(notification_id)",
        )

        return True
    except Exception as error:
        code = (
            (error.response or {}).get("Error", {}).get("Code", "")
            if hasattr(error, "response")
            else ""
        )

        if code == "ConditionalCheckFailedException":
            return False

        raise


def record_notification_result(notification_id, fields):
    notifications_table.update_item(
        Key={"notification_id": notification_id},
        UpdateExpression=(
            "SET alarm_name = :alarm_name, "
            "alarm_arn = :alarm_arn, "
            "state_value = :state_value, "
            "event_action = :event_action, "
            "pd_status = :pd_status, "
            "pd_incident_dedup_key = :dedup_key, "
            "#routing = :routing, "
            "severity = :severity, "
            "summary = :summary"
            ", pd_response_status = :pd_response_status"
            ", pd_message = :pd_message"
            ", oncall_primary = :oncall_primary"
            ", oncall_secondary = :oncall_secondary"
            ", updated_at = :updated_at"
        ),
        ExpressionAttributeNames={"#routing": "routing"},
        ExpressionAttributeValues={
            ":alarm_name": fields.get("alarm_name", ""),
            ":alarm_arn": fields.get("alarm_arn", ""),
            ":state_value": fields.get("state_value", ""),
            ":event_action": fields.get("event_action", ""),
            ":pd_status": fields.get("pd_status", ""),
            ":dedup_key": fields.get("pd_incident_dedup_key", ""),
            ":routing": fields.get("routing", ""),
            ":severity": fields.get("severity", ""),
            ":summary": fields.get("summary", ""),
            ":pd_response_status": fields.get("pd_response_status", ""),
            ":pd_message": fields.get("pd_message", ""),
            ":oncall_primary": fields.get("oncall_primary") or None,
            ":oncall_secondary": fields.get("oncall_secondary") or None,
            ":updated_at": datetime.now(timezone.utc).isoformat(),
        },
        # The reserved row always exists when we get here.
        ConditionExpression="attribute_exists(notification_id)",
    )


def handle_alarm_notification(event):
    """SNS fan-in: page PagerDuty once per CloudWatch alarm state
    change, with the current on-call attached, and land an audit row.
    Never raises for a PD/audit failure - SNS redelivery plus the
    MessageId reservation turn retries into a no-op."""
    record = event["Records"][0]

    notification_id = record.get("Sns", {}).get(
        "MessageId"
    ) or f"nosns-{uuid.uuid4()}"

    try:
        message = json.loads(record.get("Sns", {}).get("Message") or "{}")
    except (TypeError, ValueError):
        logger.exception("alarm message was not JSON")
        return {"failed": 1}

    alert = parse_alarm_notification(message)

    if not alert:
        logger.warning("unrecognized alarm message shape: %s", message)
        return {"skipped": 1}

    if not record_notification_start(notification_id):
        logger.info(
            "duplicate alarm notification %s ignored", notification_id
        )
        return {"duplicates": 1}

    state_value = alert.get("state_value", "")

    if state_value == "INSUFFICIENT_DATA":
        # Pages only cycle on ALARM <-> OK; paging on
        # INSUFFICIENT_DATA would break resolve-by-dedup lifecycle.
        try:
            record_notification_result(
                notification_id,
                {
                    "alarm_name": alert["alarm_name"],
                    "alarm_arn": alert.get("alarm_arn", ""),
                    "state_value": state_value,
                    "event_action": "none",
                    "pd_status": "skipped_insufficient_data",
                    "routing": "unrouted",
                },
            )
        except Exception:
            logger.exception(
                "could not write the audit row for %s", notification_id
            )

        return {"skipped": 1}

    secret = _pd_secret()

    if not secret:
        try:
            record_notification_result(
                notification_id,
                {
                    "alarm_name": alert["alarm_name"],
                    "alarm_arn": alert.get("alarm_arn", ""),
                    "state_value": state_value,
                    "event_action": "none",
                    "pd_status": "skipped_no_secret",
                    "routing": "unrouted",
                },
            )
        except Exception:
            logger.exception(
                "could not write the audit row for %s", notification_id
            )

        return {"skipped": 1}

    coverage = current_coverage()

    payload, routed = build_pd_alert(
        alert,
        coverage,
        secret,
        os.environ.get("STACK_NAME", ""),
    )

    pd_status = "success"
    pd_response_status = ""
    pd_message = ""
    routing = "routed" if routed else "unrouted"

    try:
        status, response_body = http(
            "POST",
            PD_EVENTS_API_URL,
            {"Content-Type": "application/json"},
            payload,
        )

        pd_response_status = str(status)
        pd_message = (
            response_body.get("status", "")
            if isinstance(response_body, dict)
            else str(response_body)
        )

        if status >= 300:
            pd_status = "failed"
    except Exception:
        logger.exception("PagerDuty Events API call failed")
        pd_status = "failed"

    fields = {
        "alarm_name": alert["alarm_name"],
        "alarm_arn": alert.get("alarm_arn", ""),
        "state_value": state_value,
        "event_action": payload["event_action"],
        "pd_status": pd_status,
        "pd_incident_dedup_key": payload["dedup_key"],
        "routing": routing,
        "severity": payload["payload"]["severity"],
        "summary": payload["payload"]["summary"],
        "pd_response_status": pd_response_status,
        "pd_message": pd_message,
        "oncall_primary": _stamp_engineer(coverage.get("primary")),
        "oncall_secondary": _stamp_engineer(coverage.get("secondary")),
    }

    try:
        record_notification_result(notification_id, fields)
    except Exception:
        logger.exception(
            "could not write the audit row for %s", notification_id
        )

    return {"paged": 1 if pd_status == "success" else 0}


def _stamp_engineer(shift):
    if not shift:
        return None

    return {
        "email": shift.get("engineer_email", ""),
        "name": shift.get("engineer_name", ""),
        "pagerduty_user_id": shift.get("pagerduty_user_id") or "",
    }


# ---- coverage / shifts ------------------------------------------------

def current_coverage(now=None):
    """The live coverage query: bounded per-role GSI windows, then the
    pure resolver. Used by /oncall/current and before every page."""
    if now is None:
        now = datetime.now(timezone.utc)

    cutoff = (now - timedelta(hours=MAX_SHIFT_HOURS)).isoformat()
    now_iso = now.isoformat()

    shifts = []

    for role in SHIFT_ROLES:
        try:
            result = table.query(
                IndexName="role-start-index",
                # One condition per key: chaining .gt/.lte on the same
                # key is rejected by DynamoDB outright, so the window
                # uses a single .between.
                KeyConditionExpression=(
                    Key("role").eq(role)
                    & Key("start_at").between(cutoff, now_iso)
                ),
                FilterExpression=Attr("end_at").gt(now_iso),
                Limit=50,
            )

            shifts.extend(result.get("Items", []))
        except Exception:
            logger.exception("coverage query failed for role %s", role)

    return resolve_coverage(
        normalize_shifts(shifts), now
    )


def normalize_shifts(items):
    """Rows leaving DynamoDB may carry Decimal/bytes shapes; the pure
    helpers only need the string fields."""
    cleaned = []

    for item in items or []:
        cleaned.append(
            {
                "shift_id": str(item.get("shift_id", "")),
                "engineer_email": str(item.get("engineer_email", "")),
                "engineer_name": str(item.get("engineer_name", "") or ""),
                "pagerduty_user_id": str(
                    item.get("pagerduty_user_id", "") or ""
                ),
                "role": str(item.get("role", "")),
                "start_at": str(item.get("start_at", "") or ""),
                "end_at": str(item.get("end_at", "") or ""),
            }
        )

    return cleaned


def create_shift(event):
    if not is_approver(event):
        return response(
            403,
            {"error": "Approver role required for this action"},
        )

    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return response(400, {"error": "Request body must be valid JSON"})

    errors, cleaned = validate_shift_input(body)

    if errors:
        return response(400, {"error": "; ".join(errors)})

    now_iso = datetime.now(timezone.utc).isoformat()

    item = {
        "shift_id": str(uuid.uuid4()),
        **cleaned,
        "created_by": actor_from_event(event),
        "created_at": now_iso,
        "updated_at": now_iso,
        "sync_status": "pending",
    }

    table.put_item(Item=item)

    sync_shift_to_pd(item, "upsert")

    try:
        refreshed = table.get_item(Key={"shift_id": item["shift_id"]})
        item = refreshed.get("Item", item)
    except Exception:
        logger.exception("could not re-read the shift after sync")

    return response(201, item)


def update_shift(shift_id, event):
    if not is_approver(event):
        return response(
            403,
            {"error": "Approver role required for this action"},
        )

    try:
        existing = table.get_item(Key={"shift_id": shift_id}).get("Item")
    except Exception:
        logger.exception("could not read the shift")
        return response(500, {"error": "Failed to read the shift"})

    if not existing:
        return response(404, {"error": "Shift not found"})

    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return response(400, {"error": "Request body must be valid JSON"})

    errors, cleaned = validate_shift_input(body)

    if errors:
        return response(400, {"error": "; ".join(errors)})

    item = {
        **existing,
        **cleaned,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "sync_status": "pending",
    }

    table.put_item(Item=item)

    sync_shift_to_pd(item, "upsert")

    try:
        refreshed = table.get_item(Key={"shift_id": shift_id})
        item = refreshed.get("Item", item)
    except Exception:
        logger.exception("could not re-read the shift after sync")

    return response(200, item)


def delete_shift(shift_id, event):
    if not is_approver(event):
        return response(
            403,
            {"error": "Approver role required for this action"},
        )

    try:
        existing = table.get_item(Key={"shift_id": shift_id}).get("Item")
    except Exception:
        logger.exception("could not read the shift")
        return response(500, {"error": "Failed to read the shift"})

    if not existing:
        return response(404, {"error": "Shift not found"})

    table.delete_item(Key={"shift_id": shift_id})

    sync_shift_to_pd(existing, "remove")

    return response(200, {"deleted": shift_id})


def list_shifts(event):
    """GET /oncall/shifts - optional limit/next_token pagination plus
    window (`from`/`to`) and engineer filters. Without limit, the
    historical collect-all behavior is kept."""
    query = event.get("queryStringParameters") or {}

    try:
        limit, start_key = parse_page_params(query)
    except ValueError as error:
        return response(400, {"error": str(error)})

    index_name = "engineer-start-index"
    key_field = "engineer_email"
    key_value = query.get("engineer")

    if not key_value:
        index_name = "role-start-index"
        key_field = "role"

        role = (query.get("role") or ROLE_PRIMARY).strip().lower()
        key_value = role

    try:
        query_kwargs = {
            "IndexName": index_name,
            "KeyConditionExpression": Key(key_field).eq(key_value),
        }

        if start_key:
            query_kwargs["ExclusiveStartKey"] = start_key

        if limit:
            query_kwargs["Limit"] = limit

        items = []
        last_key = None

        while True:
            result = table.query(**query_kwargs)

            items.extend(result.get("Items", []))

            last_key = result.get("LastEvaluatedKey")

            if not isinstance(last_key, dict):
                break

            query_kwargs["ExclusiveStartKey"] = last_key

            if limit and len(items) >= limit:
                break

        items = filter_shift_window(
            normalize_shifts(items),
            query.get("from"),
            query.get("to"),
        )

        body = {"shifts": items}

        if limit and last_key:
            body["next_token"] = encode_page_token(last_key)

        return response(200, body)
    except Exception:
        logger.exception("failed to list shifts")
        return response(500, {"error": "Failed to list shifts"})


def filter_shift_window(items, from_raw, to_raw):
    from_at = _parse_iso(from_raw) if from_raw else None
    to_at = _parse_iso(to_raw) if to_raw else None

    if from_at is None and to_at is None:
        return items

    kept = []

    for item in items:
        start = _parse_iso(item.get("start_at"))
        end = _parse_iso(item.get("end_at"))

        if start is None or end is None:
            continue

        if from_at is not None and end <= from_at:
            continue

        if to_at is not None and start >= to_at:
            continue

        kept.append(item)

    return kept


def who_am_i(event):
    return response(
        200,
        {
            "principal": principal_from_event(event),
            "can_manage": is_approver(event),
        },
    )


def coverage_route(event):
    return response(200, current_coverage())


# ---- PagerDuty schedule sync ------------------------------------------

def sync_shift_to_pd(shift, action="upsert"):
    """Best-effort push of one shift into the PagerDuty schedule
    overrides. Any failure stamps sync_status/sync_error on the row
    and never propagates - the HTTP route or alarm run must not fail
    because PagerDuty hiccuped."""
    schedule_id = os.environ.get("PD_SCHEDULE_ID") or ""

    status = "failed"
    error = ""
    synced_at = ""

    try:
        if not schedule_id:
            status = "skipped_no_schedule"
        elif not shift.get("pagerduty_user_id"):
            status = "skipped_missing_pd_user_id"
        elif action == "remove":
            status, error = sync_remove_override(shift, schedule_id)
        else:
            status, error = sync_upsert_override(shift, schedule_id)
    except Exception as sync_error:
        logger.exception("PagerDuty sync failed for shift %s",
                         shift.get("shift_id"))
        status = "failed"
        error = str(sync_error)

    # Every outcome lands on the row: synced / skipped_* / failed, so
    # the operator sees why a shift never reached the schedule.
    _stamp_sync(shift["shift_id"], status, error)

    return status


def sync_upsert_override(shift, schedule_id):
    secret = _pd_secret()

    if not secret:
        return "skipped_no_secret", ""

    overrides = [
        {
            "start": shift["start_at"],
            "end": shift["end_at"],
            "user": {
                "id": shift["pagerduty_user_id"],
                "type": PD_USER_REFERENCE,
            },
        }
    ]

    status, body = http(
        "POST",
        f"{PD_REST_API_URL}/schedules/{schedule_id}/overrides",
        _pd_headers(secret),
        {"overrides": overrides},
    )

    if status >= 300:
        return "failed", f"POST overrides -> HTTP {status}: {body}"

    return "synced", ""


def sync_remove_override(shift, schedule_id):
    secret = _pd_secret()

    if not secret:
        return "skipped_no_secret", ""

    status, body = http(
        "GET",
        f"{PD_REST_API_URL}/schedules/{schedule_id}/overrides"
        f"?since={shift['start_at']}&until={shift['end_at']}",
        _pd_headers(secret),
        None,
    )

    if status >= 300:
        return "failed", f"GET overrides -> HTTP {status}: {body}"

    removed = 0

    for override in (body or {}).get("overrides", []):
        user = override.get("user") or {}

        if user.get("id") != shift.get("pagerduty_user_id"):
            continue

        delete_status, delete_body = http(
            "DELETE",
            f"{PD_REST_API_URL}/schedules/{schedule_id}/overrides/"
            f"{override['id']}",
            _pd_headers(secret),
            None,
        )

        if delete_status >= 300:
            return (
                "failed",
                f"DELETE override {override['id']}"
                f" -> HTTP {delete_status}: {delete_body}",
            )

        removed += 1

    if not removed:
        return "skipped_no_schedule", (
            "no matching PagerDuty override found"
        )

    return "removed", ""


def reconcile_pd_schedule():
    """The daily cron + POST /oncall/sync path: diff the next 30 days
    of PagerDuty overrides against our future shifts and converge. We
    add overrides for shifts missing them and remove stale overrides
    whose window no longer matches any shift of the same engineer."""
    secret = _pd_secret()

    schedule_id = os.environ.get("PD_SCHEDULE_ID") or ""

    if not secret:
        logger.warning("PagerDuty secret missing; nothing to reconcile")
        return {"pd_status": "skipped_no_secret"}

    if not schedule_id:
        return {"pd_status": "skipped_no_schedule"}

    now = datetime.now(timezone.utc)
    until = (now + timedelta(days=30)).isoformat()

    status, body = http(
        "GET",
        f"{PD_REST_API_URL}/schedules/{schedule_id}/overrides"
        f"?since={now.isoformat()}&until={until}",
        _pd_headers(secret),
        None,
    )

    if status >= 300:
        return {
            "pd_status": "failed",
            "detail": f"GET overrides -> HTTP {status}",
        }

    pd_overrides = {
        (
            override.get("start", ""),
            override.get("end", ""),
            (override.get("user") or {}).get("id", ""),
        ): override.get("id")
        for override in (body or {}).get("overrides", [])
    }

    shifts = _future_shifts(now)

    desired = {
        (
            shift["start_at"],
            shift["end_at"],
            shift.get("pagerduty_user_id") or "",
        ): shift
        for shift in shifts
        if shift.get("pagerduty_user_id")
    }

    added = 0
    removed = 0
    skipped = 0
    failed = 0

    for key, shift in desired.items():
        if key in pd_overrides:
            continue

        if not shift.get("pagerduty_user_id"):
            skipped += 1
            continue

        sync_status = sync_shift_to_pd(shift, "upsert")

        if sync_status == "failed":
            failed += 1
        else:
            added += 1

    known_engineers = {
        shift.get("pagerduty_user_id") or ""
        for shift in _all_shifts()
    } - {""}

    for key, override_id in pd_overrides.items():
        pd_user_id = key[2]

        if pd_user_id not in known_engineers:
            continue

        if key in desired:
            continue

        delete_status, delete_body = http(
            "DELETE",
            f"{PD_REST_API_URL}/schedules/{schedule_id}/overrides/"
            f"{override_id}",
            _pd_headers(secret),
            None,
        )

        if delete_status >= 300:
            failed += 1
            logger.warning(
                "stale override %s could not be removed: %s",
                override_id,
                delete_body,
            )
        else:
            removed += 1

    logger.info(
        "PagerDuty reconcile: added=%d removed=%d skipped=%d failed=%d",
        added,
        removed,
        skipped,
        failed,
    )

    return {
        "pd_status": "success" if not failed else "failed",
        "added": added,
        "removed": removed,
        "skipped": skipped,
        "failed": failed,
    }


def _future_shifts(now):
    now_iso = now.isoformat()

    items = []

    try:
        result = table.query(
            IndexName="role-start-index",
            KeyConditionExpression=(
                Key("role").eq(ROLE_PRIMARY)
                & Key("start_at").gte(now_iso)
            ),
        )

        items.extend(result.get("Items", []))

        result = table.query(
            IndexName="role-start-index",
            KeyConditionExpression=(
                Key("role").eq(ROLE_SECONDARY)
                & Key("start_at").gte(now_iso)
            ),
        )

        items.extend(result.get("Items", []))
    except Exception:
        logger.exception("could not read future shifts")

    return normalize_shifts(items)


def _all_shifts():
    items = []

    try:
        scan = table.scan()

        items.extend(scan.get("Items", []))
    except Exception:
        logger.exception("could not scan shifts for known engineers")

    return normalize_shifts(items)


def _stamp_sync(shift_id, status, error):
    try:
        table.update_item(
            Key={"shift_id": shift_id},
            UpdateExpression=(
                "SET sync_status = :status, "
                "last_synced_at = :at"
                + (
                    ", sync_error = :error"
                    if error
                    else " REMOVE sync_error"
                )
            ),
            ExpressionAttributeValues={
                ":status": status,
                ":at": datetime.now(timezone.utc).isoformat(),
                ":error": error,
            },
        )
    except Exception:
        logger.exception("could not stamp sync status on %s", shift_id)


def sync_route(event):
    if not is_approver(event):
        return response(
            403,
            {"error": "Approver role required for this action"},
        )

    try:
        result = reconcile_pd_schedule()
    except Exception:
        logger.exception("manual reconcile failed")
        return response(
            500,
            {"error": "Failed to reconcile the PagerDuty schedule"},
        )

    return response(200, result)


# ---- notification audit ----------------------------------------------

def list_notifications(event):
    query = event.get("queryStringParameters") or {}

    try:
        limit, start_key = parse_page_params(query)
    except ValueError as error:
        return response(400, {"error": str(error)})

    alarm_name = query.get("alarm")

    try:
        query_kwargs = {
            "ScanIndexForward": False,
        }

        if alarm_name:
            query_kwargs["IndexName"] = "alarm-index"
            query_kwargs["KeyConditionExpression"] = Key(
                "alarm_name"
            ).eq(alarm_name)
        else:
            query_kwargs["IndexName"] = "time-index"
            query_kwargs["KeyConditionExpression"] = Key(
                "ts_key"
            ).eq("NOTIF")

        if start_key:
            query_kwargs["ExclusiveStartKey"] = start_key

        if limit:
            query_kwargs["Limit"] = limit

        items = []
        last_key = None

        while True:
            result = notifications_table.query(**query_kwargs)

            items.extend(result.get("Items", []))

            last_key = result.get("LastEvaluatedKey")

            if not isinstance(last_key, dict):
                break

            query_kwargs["ExclusiveStartKey"] = last_key

            if limit and len(items) >= limit:
                break

        body = {"notifications": items}

        if limit and last_key:
            body["next_token"] = encode_page_token(last_key)

        return response(200, body)
    except Exception:
        logger.exception("failed to list notifications")
        return response(
            500,
            {"error": "Failed to list notifications"},
        )


# ---- handler ----------------------------------------------------------

def lambda_handler(event, context):
    record = (
        event.get("Records", [{}])[0] if isinstance(event.get("Records"), list) else None
    )

    if record and record.get("EventSource") == "aws:sns":
        return handle_alarm_notification(event)

    if event.get("source") == "aws.events":
        return reconcile_pd_schedule() or {}

    method = event.get("requestContext", {}).get("http", {}).get("method")
    path = event.get("rawPath", "")

    if method == "GET" and path == "/oncall/me":
        return who_am_i(event)

    if method == "GET" and path == "/oncall/current":
        return coverage_route(event)

    if method == "GET" and path == "/oncall/shifts":
        return list_shifts(event)

    if method == "POST" and path == "/oncall/shifts":
        return create_shift(event)

    if (
        method == "PUT"
        and path.startswith("/oncall/shifts/")
        and path.count("/") == 3
    ):
        return update_shift(path.split("/")[-1], event)

    if (
        method == "DELETE"
        and path.startswith("/oncall/shifts/")
        and path.count("/") == 3
    ):
        return delete_shift(path.split("/")[-1], event)

    if method == "POST" and path == "/oncall/sync":
        return sync_route(event)

    if method == "GET" and path == "/oncall/notifications":
        return list_notifications(event)

    return response(404, {"error": "Not found"})