# On-Call & PagerDuty Integration

Design record for the on-call feature added after the hardening pass.
It exists because the operational alarms had **no subscriber**: every
error alarm published to `AlarmsTopic`, and with no email attached it
went nowhere. An unattended alarm is indistinguishable from no alarm.

## Pipeline

```
 Lambda error / API 5XX
            |
            v
 CloudWatch Alarm            (alarm-name per function + api-5xx)
            |
            v  (classic CW SNS JSON: AlarmName, NewStateValue,
            |    NewStateReason, StateChangeTime, Trigger)
 AlarmsTopic (SNS)
            |
            v
 OnCallFunction  --1--> OncallNotificationsTable   (audit, idempotent)
            |
            +--2--> resolve_coverage(shifts, now)   (half-open window)
            |
            +--3--> PagerDuty Events v2
                    POST https://events.pagerduty.com/v2/enqueue
                    {
                      routing_key:      <from Secrets Manager>,
                      event_action:     trigger | resolve,
                      dedup_key:        <alarm ARN>,
                      payload: { summary, source, severity,
                                 custom_details: { account, region,
                                 stack, metric, state_change,
                                 oncall: {...},
                                 oncall_coverage } }
                    }
```

## Lifecycle (dedup key = alarm ARN)

| Alarm state | PD `event_action` | PD severity | Notes |
|---|---|---|---|
| `ALARM` | `trigger` | `critical` | Fires the escalation policy |
| `OK` | `resolve` | `info` | Summary suffixed `[RESOLVED]`; same dedup key resolves the incident created from `ALARM` |
| `INSUFFICIENT_DATA` | (no call) | — | Paging on it would break the ALARM↔OK resolve lifecycle; audit-only skip |

The classic CloudWatch SNS message shape is parsed first; the
EventBridge `detail.alarmName` envelope shape is handled defensively
too. Unrecognized shapes are logged and skipped as `{skipped: 1}`.

## Coverage resolution

`resolve_coverage(shifts, now)` is pure and unit-tested:

- half-open windows: `start_at <= now < end_at` — a shift that starts
  exactly now covers; one that ends exactly now does not.
- per role (`primary`, `secondary`) the **newest `start_at`** covering
  shift wins; the losers appear in `overlapping[]`.
- shift creation caps the window at `MAX_SHIFT_HOURS = 168` (7 days),
  and the GSI query bounds its scan with
  `cutoff = now - MAX_SHIFT_HOURS - 1h`, so coverage lookups are
  bounded queries, never table scans.
- **No coverage is not a silence policy**: the page still fires with
  `oncall_coverage: "UNROUTED: no shift covers this time"` and the
  audit row records `routing: "unrouted"`. PagerDuty itself is the
  human fallback (the operator's own PD user / escalation policy
  receives it).

## Audit + idempotency

SNS delivery is at-least-once. `notification_id` = the SNS
`MessageId`; `handle_alarm_notification` first does a **conditional
PutItem** (`attribute_not_exists(notification_id)`, reserved with
`pd_status: "processing"`). A redelivery that loses the race gets
`{duplicates: 1}` and never calls PagerDuty twice. Winners finish and
stamp `pd_status` (`success` / `skipped_*` / `failed`),
`pd_incident_dedup_key`, routing and the on-call entries at page
time.

Any PagerDuty or audit failure is **recorded, never raised** — SNS
retries a raise into a redelivery, which idempotency would then skip,
and a paging Lambda must not become the newest error alarm.

## Two-way rotation sync

Our shift table is the source of truth; PagerDuty schedule overrides
are the projection.

- Create/update → `POST /schedules/{id}/overrides` with
  `From` header + `Authorization: Token token=<api_token>` +
  `Accept: application/vnd.pagerduty+json;version=2`
  (the Overrides API requires the `From` identity, a PD user allowed
  to edit the schedule, and a `user_reference` — hence every shift
  carries an optional `pagerduty_user_id`; without it, sync degrades
  to `skipped_missing_pd_user_id` and nothing breaks).
- Delete → list overrides in the window, `DELETE` the one whose user
  id matches; none found → `skipped_no_schedule` with an explanatory
  `sync_error` on the row.
- `reconcile_pd_schedule()` (daily EventBridge `rate(1 day)` + the
  admins' `POST /oncall/sync`) diffs the next 30 days of PD overrides
  against our future shifts, keyed by
  `(start_at, end_at, pd_user_id)`, adds missing overrides and removes
  stale ones — but only stale overrides whose PD user id belongs to a
  known on-call engineer (foreign overrides are never touched).
- Every outcome is stamped onto the shift row (`sync_status`,
  `sync_error`, `last_synced_at`) and surfaced in the UI.

Escalation, acknowledgment, and SMS are deliberately PagerDuty's —
this stack neither rebuilds nor duplicates them.

## Credential lifecycle

`PagerDutySecret` is created **empty** on purpose (`AWS::SecretsManager::Secret`
without `SecretString`/`GenerateSecretString`): CloudFormation never
holds a PagerDuty credential, never echoes one in outputs, and the
runtime role only ever gets `GetSecretValue`/`DescribeSecret` — never
the value-writing actions. While it is empty:

- alarm pages record `skipped_no_secret` audit rows (no PD call), and
- every sync records `skipped_no_secret`.

Nothing raises; filling the secret (README command, using
`PagerDutySecretArn`) turns the feature on. `PD_SCHEDULE_ID` empty
behaves the same way (`skipped_no_schedule`). The secret is cached per
Lambda cold start to keep paging latency low.