// Pure on-call rules — the layer behind the OnCall page, kept here so
// the scheduling rules are unit-testable without a render. The backend
// (backend/lambdas/oncall/app.py) owns the data; these helpers mirror
// its single coverage definition so the UI never contradicts it:
// half-open windows (start_at <= now < end_at), and per role the
// newest covering start_at wins.

import type {
  ApiOnCallCoverage,
  ApiOnCallShift,
  ApiOnCallShiftInput,
  OnCallRole,
  OnCallSyncStatus,
} from "../types";
import { formatDateTime } from "./format";

/** A shift covers `now` iff start_at <= now < end_at (half-open).
    Rows with unparsable timestamps are excluded, exactly like the
    backend's _parse_iso filter. `now` is an ISO string or Date. */
export function shiftsCoveringNow(
  shifts: ApiOnCallShift[],
  now: string | Date,
): ApiOnCallShift[] {
  const at = new Date(now).getTime();
  if (Number.isNaN(at)) return [];
  return shifts.filter((shift) => {
    const start = new Date(shift.start_at).getTime();
    const end = new Date(shift.end_at).getTime();
    if (Number.isNaN(start) || Number.isNaN(end)) return false;
    return start <= at && at < end;
  });
}

/** The live coverage resolution over a schedule — the same shape the
    GET /oncall/current route returns. Per role the newest covering
    start_at wins; the shift it displaces (or a later-starting shift
    it loses to) is carried in `overlapping` for the UI warning. */
export function resolveOnCall(
  shifts: ApiOnCallShift[],
  now: string | Date,
): ApiOnCallCoverage {
  const at = new Date(now);
  const covering = shiftsCoveringNow(shifts, at);

  const byRole: { primary?: ApiOnCallShift; secondary?: ApiOnCallShift } = {};
  const overlapping: ApiOnCallShift[] = [];

  for (const shift of covering) {
    if (shift.role !== "primary" && shift.role !== "secondary") continue;

    const current = byRole[shift.role];
    if (current === undefined) {
      byRole[shift.role] = shift;
      continue;
    }

    if (new Date(shift.start_at) > new Date(current.start_at)) {
      overlapping.push(current);
      byRole[shift.role] = shift;
    } else {
      overlapping.push(shift);
    }
  }

  return {
    now: at.toISOString(),
    primary: byRole.primary ?? null,
    secondary: byRole.secondary ?? null,
    overlapping,
    coverage: byRole.primary || byRole.secondary ? "covered" : "none",
  };
}

/** Schedule warnings for the table: the shift_ids of any pair of
    same-role shifts whose half-open windows intersect. Overlaps are a
    warning (the audit keeps the winner stamped), not an error. */
export function overlappingShiftIds(
  shifts: ApiOnCallShift[],
): Set<string> {
  const flagged = new Set<string>();
  for (let i = 0; i < shifts.length; i += 1) {
    for (let j = i + 1; j < shifts.length; j += 1) {
      const a = shifts[i];
      const b = shifts[j];
      if (a.role !== b.role) continue;
      const aStart = new Date(a.start_at).getTime();
      const aEnd = new Date(a.end_at).getTime();
      const bStart = new Date(b.start_at).getTime();
      const bEnd = new Date(b.end_at).getTime();
      if (
        Number.isNaN(aStart) ||
        Number.isNaN(aEnd) ||
        Number.isNaN(bStart) ||
        Number.isNaN(bEnd)
      ) {
        continue;
      }
      if (aStart < bEnd && bStart < aEnd) {
        flagged.add(a.shift_id);
        flagged.add(b.shift_id);
      }
    }
  }
  return flagged;
}

/** CloudWatch alarm state -> the PagerDuty severity the backend
    selected (a string-exact mirror of the Lambda's
    `pd_severity_for_state`: ALARM pages critical, OK resolves as
    info, INSUFFICIENT_DATA is warning, anything else would be an
    unrecognized state — error). */
export function pdSeverityForState(
  stateValue: string | null | undefined,
): "critical" | "info" | "warning" | "error" {
  switch (stateValue) {
    case "ALARM":
      return "critical";
    case "OK":
      return "info";
    case "INSUFFICIENT_DATA":
      return "warning";
    default:
      return "error";
  }
}

/** Friendly label (and one-line meaning) for every sync stamp. */
export const SYNC_STATUS_LABELS: Record<
  OnCallSyncStatus,
  { label: string; meaning: string }
> = {
  pending: {
    label: "Pending",
    meaning: "Waiting for the next PagerDuty sync pass",
  },
  synced: {
    label: "Synced",
    meaning: "A PagerDuty schedule override exists for this shift",
  },
  skipped_missing_pd_user_id: {
    label: "Skipped — no PD user id",
    meaning:
      "Shift has no PagerDuty user id, so no override was attempted",
  },
  skipped_no_secret: {
    label: "Skipped — no PD credentials",
    meaning: "The PagerDuty API credentials are missing, so no override was attempted",
  },
  skipped_no_schedule: {
    label: "Skipped — no PD schedule",
    meaning: "The backend has no PD_SCHEDULE_ID configured, so no override was attempted",
  },
  failed: {
    label: "Failed",
    meaning: "The PagerDuty call failed — see the recorded error",
  },
  removed: {
    label: "Removed",
    meaning: "The matching PagerDuty override was deleted",
  },
};

/** CSS color token for a sync-status badge. */
export function syncStatusColor(status: OnCallSyncStatus): string {
  switch (status) {
    case "synced":
    case "removed":
      return "var(--good)";
    case "pending":
      return "var(--info)";
    case "failed":
      return "var(--danger)";
    default:
      // skipped_*: nothing was attempted — diagnostic, not broken.
      return "var(--warning)";
  }
}

/** True when nothing reached the PagerDuty schedule because a
    precondition was missing (no user id, no credential, no schedule).
    The table renders the recorded sync_error inline for these. */
export function isSkippedSyncStatus(
  status: OnCallSyncStatus,
): boolean {
  return status.startsWith("skipped_");
}

/** One line of human text for a shift window, from the format
    helpers: the date once when the shift starts and ends on the same
    calendar day, both ends fully spelled out otherwise. */
export function formatShiftRange(
  startAt: string | null | undefined,
  endAt: string | null | undefined,
): string {
  if (!startAt || !endAt) return "—";
  const start = new Date(startAt);
  const end = new Date(endAt);
  if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())) {
    return "—";
  }
  const startText = formatDateTime(startAt);
  const endText = formatDateTime(endAt);
  if (start.toDateString() === end.toDateString()) {
    const endClock = end.toLocaleTimeString("en-US", {
      hour: "numeric",
      minute: "2-digit",
    });
    return `${startText} – ${endClock}`;
  }
  return `${startText} → ${endText}`;
}

/** UTC ISO string -> the value a <input type="datetime-local"> consumes,
    re-expressed in the viewer's local time (editing pre-fills the same
    wall clock the shift row rendered with). The round trip back
    through Date preserves the instant. */
export function toDatetimeLocalValue(
  iso: string | null | undefined,
): string {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  const pad = (value: number) => String(value).padStart(2, "0");
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
    `T${pad(date.getHours())}:${pad(date.getMinutes())}`
  );
}

export interface OnCallShiftFormInput {
  engineer_email: string;
  engineer_name: string;
  pagerduty_user_id: string;
  role: string;
  start_at: string;
  end_at: string;
}

export interface ShiftFormValidation {
  errors: string[];
  cleaned: ApiOnCallShiftInput | null;
}

const SHIFT_ROLES: OnCallRole[] = ["primary", "secondary"];

// Simple shape check only — the backend re-validates and owns the
// rules; this just keeps obvious mistakes from a round trip.
const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

/** Field-level validation for the admin shift form — mirrors the
    backend's validate_shift_input (including its 7-day / 168h cap).
    Returns every human-readable error; cleaned is null unless the
    whole input is valid, with datetimes normalized to UTC ISO-8601
    (the datetime-local input is read in the viewer's local time). */
export function validateShiftForm(
  input: OnCallShiftFormInput,
): ShiftFormValidation {
  const errors: string[] = [];

  const engineerEmail = input.engineer_email.trim();
  if (!engineerEmail) {
    errors.push("engineer_email must be a non-empty string");
  } else if (!EMAIL_PATTERN.test(engineerEmail)) {
    errors.push("engineer_email does not look like an email address");
  }

  const role = input.role.trim().toLowerCase() as OnCallRole;
  if (!SHIFT_ROLES.includes(role)) {
    errors.push(`role must be one of ${JSON.stringify(SHIFT_ROLES)}`);
  }

  const startAt = input.start_at ? new Date(input.start_at) : null;
  const endAt = input.end_at ? new Date(input.end_at) : null;
  if (startAt === null || Number.isNaN(startAt.getTime())) {
    errors.push("start_at must be an ISO-8601 datetime");
  }
  if (endAt === null || Number.isNaN(endAt.getTime())) {
    errors.push("end_at must be an ISO-8601 datetime");
  }

  let cleaned: ApiOnCallShiftInput | null = null;
  if (
    startAt !== null &&
    !Number.isNaN(startAt.getTime()) &&
    endAt !== null &&
    !Number.isNaN(endAt.getTime())
  ) {
    if (startAt >= endAt) {
      errors.push("start_at must be before end_at");
    } else if (endAt.getTime() - startAt.getTime() > 7 * 24 * 60 * 60 * 1000) {
      errors.push("a shift may span at most 7 days");
    }
  }

  if (errors.length === 0 && startAt !== null && endAt !== null) {
    cleaned = {
      engineer_email: engineerEmail,
      engineer_name: input.engineer_name.trim() || undefined,
      pagerduty_user_id: input.pagerduty_user_id.trim() || undefined,
      role,
      start_at: startAt.toISOString(),
      end_at: endAt.toISOString(),
    };
  }

  return { errors, cleaned };
}