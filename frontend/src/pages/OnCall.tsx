// OnCall — the schedule that turns a CloudWatch alarm into a real
// PagerDuty page. The backend records shifts, resolves who covers
// right now (half-open windows, newest start wins per role), pushes
// schedule overrides into PagerDuty, and stamps every alarm
// notification with the on-call engineers at page time. This page is
// the operator view of that loop: coverage, the shift table with
// per-row sync stamps, the admin shift form, a manual reconcile, and
// the paging audit.
//
// Numbers provenance: everything here comes from the /oncall routes —
// the only client-side schedule math is mirroring the backend's pure
// coverage rules (utils/oncall.ts) for the overlap warnings.

import { useCallback, useEffect, useRef, useState } from "react";
import {
  BellRing,
  Pencil,
  Plus,
  RefreshCw,
  Trash2,
  X,
} from "lucide-react";
import { api } from "../services/api";
import { useLiveSystem } from "../hooks/useLiveSystem";
import {
  EmptyState,
  ErrorState,
  InlineWarning,
  LiveBadge,
  PageHeader,
  SignedOutNote,
  Spinner,
  StatTile,
  TableSkeleton,
} from "../components/ui/atoms";
import {
  formatShiftRange,
  isSkippedSyncStatus,
  overlappingShiftIds,
  pdSeverityForState,
  SYNC_STATUS_LABELS,
  syncStatusColor,
  toDatetimeLocalValue,
  validateShiftForm,
} from "../utils/oncall";
import type { OnCallShiftFormInput } from "../utils/oncall";
import { formatDateTime, relativeTime } from "../utils/format";
import type {
  ApiOnCallCoverage,
  ApiOnCallMe,
  ApiOnCallNotification,
  ApiOnCallShift,
} from "../types";

const SHIFT_PAGE_SIZE = 25;
const NOTICE_PAGE_SIZE = 30;

const EMPTY_FORM: OnCallShiftFormInput = {
  engineer_email: "",
  engineer_name: "",
  pagerduty_user_id: "",
  role: "primary",
  start_at: "",
  end_at: "",
};

// A validation error maps to the field it names; anything else (the
// cross-field rules: start before end, the 7-day cap) renders above
// the form as a whole-form warning.
const FORM_FIELDS = [
  "engineer_email",
  "engineer_name",
  "pagerduty_user_id",
  "role",
  "start_at",
  "end_at",
] as const;

function groupFormErrors(errors: string[]) {
  const byField: Partial<Record<(typeof FORM_FIELDS)[number], string[]>> = {};
  const wholeForm: string[] = [];
  for (const error of errors) {
    const field = FORM_FIELDS.find((candidate) => error.startsWith(candidate));
    if (field) {
      byField[field] = [...(byField[field] ?? []), error];
    } else {
      wholeForm.push(error);
    }
  }
  return { byField, wholeForm };
}

function FieldError({ errors }: { errors?: string[] }) {
  if (!errors || errors.length === 0) return null;
  return (
    <div className="doc-sub" style={{ color: "var(--danger)", marginTop: 3 }}>
      {errors.map((error) => (
        <div key={error}>{error}</div>
      ))}
    </div>
  );
}

function SyncBadge({ shift }: { shift: ApiOnCallShift }) {
  const status = shift.sync_status;
  return (
    <span
      className="badge badge-neutral"
      title={SYNC_STATUS_LABELS[status]?.meaning}
      style={{ color: syncStatusColor(status) }}
    >
      {SYNC_STATUS_LABELS[status]?.label ?? status}
    </span>
  );
}

function RoutingBadge({
  routing,
}: {
  routing: "routed" | "unrouted" | undefined;
}) {
  const routed = routing === "routed";
  return (
    <span
      className={`badge ${routed ? "badge-experiment" : "badge-warning"}`}
      title={
        routed
          ? "A PagerDuty incident was created for this notification"
          : "Nobody was on call — no PagerDuty incident went out"
      }
    >
      {routed ? "Routed" : "Unrouted"}
    </span>
  );
}

/** Two-step confirm for the irreversible delete (the backend also
    removes the PagerDuty override): the first click arms the button,
    the second fires it — the approval page's ExecuteButton pattern. */
function DeleteShiftButton({
  shiftId,
  busy,
  onFire,
}: {
  shiftId: string;
  busy: boolean;
  onFire: (shiftId: string) => void;
}) {
  const [armed, setArmed] = useState(false);

  useEffect(() => {
    if (!armed) return;
    const timer = setTimeout(() => setArmed(false), 4000);
    return () => clearTimeout(timer);
  }, [armed]);

  return (
    <button
      type="button"
      className={armed ? "btn btn-sm btn-danger" : "btn btn-sm btn-ghost"}
      disabled={busy}
      title="Delete the shift and remove its PagerDuty override"
      aria-label={`Delete shift for ${shiftId}`}
      onClick={() => {
        if (!armed) {
          setArmed(true);
          return;
        }
        setArmed(false);
        onFire(shiftId);
      }}
    >
      {armed ? <Trash2 size={13} /> : <Trash2 size={12} />}
    </button>
  );
}

export default function OnCall() {
  const { status } = useLiveSystem();

  const [me, setMe] = useState<ApiOnCallMe | null>(null);
  const [coverage, setCoverage] = useState<ApiOnCallCoverage | null>(null);
  const [coverageError, setCoverageError] = useState<string | null>(null);

  const [shifts, setShifts] = useState<ApiOnCallShift[]>([]);
  const [shiftsToken, setShiftsToken] = useState<string | null>(null);
  const [shiftsLoading, setShiftsLoading] = useState(true);
  const [shiftsLoadingMore, setShiftsLoadingMore] = useState(false);
  const [shiftsError, setShiftsError] = useState<string | null>(null);

  // The alarm filter lives in a ref so picking one reloads only the
  // audit (the page-level reload callback stays stable).
  const alarmFilterRef = useRef("");
  const [alarmFilter, setAlarmFilter] = useState("");

  const [notifications, setNotifications] = useState<ApiOnCallNotification[]>([]);
  const [notifToken, setNotifToken] = useState<string | null>(null);
  const [notifLoading, setNotifLoading] = useState(true);
  const [notifLoadingMore, setNotifLoadingMore] = useState(false);
  const [notifError, setNotifError] = useState<string | null>(null);

  const [notice, setNotice] = useState<string | null>(null);
  const [syncing, setSyncing] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);

  const [formOpen, setFormOpen] = useState(false);
  const [editing, setEditing] = useState<ApiOnCallShift | null>(null);
  const [form, setForm] = useState<OnCallShiftFormInput>(EMPTY_FORM);
  const [formErrors, setFormErrors] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);

  const canManage = me?.can_manage === true;

  // ---- loaders ----

  const loadWhoAmI = useCallback(async () => {
    try {
      setMe(await api.getOnCallMe());
    } catch {
      // Whoami only gates the write controls; treat a failure as
      // read-only rather than blocking the whole page.
      setMe(null);
    }
  }, []);

  const loadCoverage = useCallback(async () => {
    setCoverageError(null);
    try {
      setCoverage(await api.getOnCallCurrent());
    } catch (error) {
      setCoverageError(
        error instanceof Error ? error.message : "Failed to load coverage",
      );
    }
  }, []);

  const loadShifts = useCallback(async () => {
    setShiftsLoading(true);
    setShiftsError(null);
    try {
      const result = await api.listOnCallShifts({ limit: SHIFT_PAGE_SIZE });
      setShifts(result.shifts);
      setShiftsToken(result.next_token ?? null);
    } catch (error) {
      setShiftsError(
        error instanceof Error ? error.message : "Failed to load shifts",
      );
    } finally {
      setShiftsLoading(false);
    }
  }, []);

  const loadMoreShifts = useCallback(async () => {
    if (!shiftsToken || shiftsLoadingMore) return;
    setShiftsLoadingMore(true);
    try {
      const result = await api.listOnCallShifts({
        limit: SHIFT_PAGE_SIZE,
        next_token: shiftsToken,
      });
      setShifts((current) => [...current, ...result.shifts]);
      setShiftsToken(result.next_token ?? null);
    } catch (error) {
      setNotice(
        error instanceof Error ? error.message : "Failed to load more shifts",
      );
    } finally {
      setShiftsLoadingMore(false);
    }
  }, [shiftsToken, shiftsLoadingMore]);

  const loadNotifications = useCallback(async () => {
    setNotifLoading(true);
    setNotifError(null);
    try {
      const result = await api.listOnCallNotifications({
        limit: NOTICE_PAGE_SIZE,
        alarm: alarmFilterRef.current || undefined,
      });
      setNotifications(result.notifications);
      setNotifToken(result.next_token ?? null);
    } catch (error) {
      setNotifError(
        error instanceof Error
          ? error.message
          : "Failed to load the paging audit",
      );
    } finally {
      setNotifLoading(false);
    }
  }, []);

  const loadMoreNotifications = useCallback(async () => {
    if (!notifToken || notifLoadingMore) return;
    setNotifLoadingMore(true);
    try {
      const result = await api.listOnCallNotifications({
        limit: NOTICE_PAGE_SIZE,
        next_token: notifToken,
        alarm: alarmFilterRef.current || undefined,
      });
      setNotifications((current) => [...current, ...result.notifications]);
      setNotifToken(result.next_token ?? null);
    } catch (error) {
      setNotice(
        error instanceof Error
          ? error.message
          : "Failed to load more notifications",
      );
    } finally {
      setNotifLoadingMore(false);
    }
  }, [notifToken, notifLoadingMore]);

  const reload = useCallback(async () => {
    await Promise.all([
      loadWhoAmI(),
      loadCoverage(),
      loadShifts(),
      loadNotifications(),
    ]);
  }, [loadWhoAmI, loadCoverage, loadShifts, loadNotifications]);

  useEffect(() => {
    if (status === "online") void reload();
  }, [status, reload]);

  // ---- write actions ----

  const deleteShift = async (shiftId: string) => {
    setBusyId(shiftId);
    setNotice(null);
    try {
      const result = await api.deleteOnCallShift(shiftId);
      setNotice(
        `Shift ${result.deleted} deleted — its PagerDuty override was removed too.`,
      );
      await Promise.all([loadCoverage(), loadShifts()]);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Delete failed");
    } finally {
      setBusyId(null);
    }
  };

  const sync = async () => {
    setSyncing(true);
    setNotice(null);
    try {
      const result = await api.syncOnCall();
      if (result.pd_status === "success") {
        setNotice(
          `PagerDuty sync succeeded — ${result.added ?? 0} override` +
            `${(result.added ?? 0) === 1 ? "" : "s"} added, ${result.removed ?? 0} removed` +
            `, ${result.skipped ?? 0} skipped, ${result.failed ?? 0} failed.`,
        );
      } else if (result.pd_status.startsWith("skipped")) {
        setNotice(
          result.pd_status === "skipped_no_secret"
            ? "PagerDuty sync skipped — no PagerDuty API credentials are configured, so the schedule was not touched."
            : "PagerDuty sync skipped — no PagerDuty schedule id is configured, so nothing could be pushed.",
        );
      } else {
        setNotice(
          `PagerDuty sync failed — ${result.failed ?? 0} override call` +
            `${(result.failed ?? 1) === 1 ? "" : "s"} errored` +
            `${result.detail ? `: ${result.detail}` : ""}. ` +
            "The per-shift stamps in the schedule table record what happened.",
        );
      }
      await Promise.all([loadCoverage(), loadShifts()]);
    } catch (error) {
      setNotice(
        error instanceof Error ? error.message : "PagerDuty sync failed",
      );
    } finally {
      setSyncing(false);
    }
  };

  const openCreate = () => {
    setEditing(null);
    setForm(EMPTY_FORM);
    setFormErrors([]);
    setFormOpen(true);
  };

  const openEdit = (shift: ApiOnCallShift) => {
    setEditing(shift);
    setForm({
      engineer_email: shift.engineer_email,
      engineer_name: shift.engineer_name ?? "",
      pagerduty_user_id: shift.pagerduty_user_id ?? "",
      role: shift.role,
      start_at: toDatetimeLocalValue(shift.start_at),
      end_at: toDatetimeLocalValue(shift.end_at),
    });
    setFormErrors([]);
    setFormOpen(true);
  };

  const saveShift = async () => {
    const validation = validateShiftForm(form);
    setFormErrors(validation.errors);
    if (!validation.cleaned) return;

    setSaving(true);
    setNotice(null);
    try {
      const result = editing
        ? await api.updateOnCallShift(editing.shift_id, validation.cleaned)
        : await api.createOnCallShift(validation.cleaned);
      setNotice(
        `${editing ? "Updated" : "Created"} ${result.shift.role} shift for ` +
          `${result.shift.engineer_name || result.shift.engineer_email} ` +
          `(${formatShiftRange(result.shift.start_at, result.shift.end_at)}).`,
      );
      setFormOpen(false);
      await Promise.all([loadCoverage(), loadShifts()]);
    } catch (error) {
      // A 403 here is the backend re-checking the Approver role — its
      // exact message shows in the notice box rather than a dead form.
      setNotice(
        error instanceof Error ? error.message : "Saving the shift failed",
      );
    } finally {
      setSaving(false);
    }
  };

  const pickAlarmFilter = (next: string) => {
    alarmFilterRef.current = next;
    setAlarmFilter(next);
    void loadNotifications();
  };

  const grouped = groupFormErrors(formErrors);

  // Overlap flags for the schedule table — the same pure rules the
  // backend applies when it resolves coverage.
  const liveOverlaps = overlappingShiftIds(shifts);

  // Alarm names the audit has actually seen — the filter dropdown is
  // built from recorded rows, not invented ones.
  const alarmNames = Array.from(
    new Set(notifications.map((notification) => notification.alarm_name)),
  ).sort();

  // "Right now" is always the backend's live answer — no client-side
  // schedule resolution beyond the overlap warnings in the table.
  const currentResolution = coverage;

  const renderEngineerTile = (
    label: string,
    shift: ApiOnCallShift | null,
    none: string,
  ) => (
    <StatTile
      label={label}
      value={
        shift ? shift.engineer_name || shift.engineer_email || "—" : "—"
      }
      footer={
        shift ? (
          <span className="muted" style={{ fontSize: 11 }}>
            <span className="tnum" style={{ display: "block" }}>
              {shift.engineer_email}
            </span>
            {formatShiftRange(shift.start_at, shift.end_at)}
          </span>
        ) : (
          <span className="muted" style={{ fontSize: 11 }}>
            {none}
          </span>
        )
      }
    />
  );

  return (
    <div className="page-in">
      <PageHeader
        title="On-Call"
        subtitle="The shift schedule behind PagerDuty sync: who a firing alarm pages right now, whether each override reached the schedule, and the paging audit for past alarms."
      >
        <LiveBadge />
        {canManage && (
          <>
            <button
              type="button"
              className="btn"
              onClick={sync}
              disabled={syncing}
              title="Diff the next 30 days of PagerDuty overrides against the recorded future shifts and converge"
            >
              <RefreshCw size={14} />
              {syncing ? "Syncing…" : "Reconcile with PagerDuty"}
            </button>
            <button
              type="button"
              className="btn btn-primary"
              onClick={openCreate}
              title="Record a new on-call shift"
            >
              <Plus size={14} />
              New shift
            </button>
          </>
        )}
        <button
          type="button"
          className="btn"
          onClick={reload}
          disabled={shiftsLoading || notifLoading || status === "connecting"}
        >
          Refresh
        </button>
      </PageHeader>

      {notice && (
        <div
          className="row"
          role="status"
          style={{
            border: "1px solid var(--border-strong)",
            borderRadius: "var(--radius-sm)",
            padding: "8px 12px",
            fontSize: 12.5,
            color: "var(--text-2)",
            marginBottom: 12,
            gap: 8,
          }}
        >
          <BellRing size={14} style={{ color: "var(--accent-strong)" }} />
          <span>{notice}</span>
          <button
            type="button"
            className="btn btn-sm btn-ghost"
            style={{ marginLeft: "auto" }}
            onClick={() => setNotice(null)}
            aria-label="Dismiss notice"
          >
            <X size={12} />
          </button>
        </div>
      )}

      {status === "online" && me !== null && !me.can_manage && (
        <div className="row" style={{ marginBottom: 14 }}>
          <InlineWarning title="Read-only — only Approvers can manage shifts">
            Your account cannot create, edit or reconcile shifts. The schedule
            below is still the source of truth for who a firing alarm pages.
          </InlineWarning>
        </div>
      )}

      {status === "signed-out" && (
        <div className="panel">
          <SignedOutNote />
        </div>
      )}

      {status === "offline" && (
        <div className="panel">
          <ErrorState onRetry={reload} />
        </div>
      )}

      {status === "unconfigured" && (
        <div className="panel">
          <EmptyState
            title="No API base URL configured"
            description="Point VITE_API_BASE_URL at your deployed API Gateway endpoint (see frontend/.env.example) to see on-call coverage."
          />
        </div>
      )}

      {status === "online" && (
        <>
          {/* Right now: who would a firing alarm page? */}
          <section className="panel panel-pad" style={{ marginBottom: 14 }}>
            <div
              className="row"
              style={{ justifyContent: "space-between", marginBottom: 10 }}
            >
              <strong style={{ fontSize: 15 }}>Right now</strong>
              {currentResolution && (
                <span
                  className={`badge ${currentResolution.coverage === "covered" ? "badge-experiment" : "badge-danger"}`}
                  title={
                    currentResolution.coverage === "covered"
                      ? "At least one role covers now — a firing alarm pages"
                      : "No shift covers now — a firing alarm leaves PagerDuty unrouted"
                  }
                >
                  {currentResolution.coverage === "covered"
                    ? "Covered"
                    : "UNROUTED — no on-call"}
                </span>
              )}
            </div>
            {coverageError ? (
              <ErrorState
                title="Coverage unavailable"
                description={coverageError}
                onRetry={loadCoverage}
              />
            ) : currentResolution === null ? (
              <div style={{ padding: 8 }}>
                <Spinner />
              </div>
            ) : (
              <>
                <div
                  style={{
                    display: "grid",
                    gap: 12,
                    gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))",
                  }}
                >
                  {renderEngineerTile(
                    "Primary",
                    currentResolution.primary,
                    "no primary shift covers now",
                  )}
                  {renderEngineerTile(
                    "Secondary",
                    currentResolution.secondary,
                    "no secondary shift covers now",
                  )}
                </div>
                {currentResolution.overlapping.length > 0 && (
                  <div style={{ marginTop: 12 }}>
                    <InlineWarning title="Overlapping shifts recorded">
                      {currentResolution.overlapping.length} covering shift
                      {currentResolution.overlapping.length === 1 ? "" : "s"}{" "}
                      lost the role to a newer start — the newest start_at wins,
                      and the audit stamps the winner on every page.
                    </InlineWarning>
                  </div>
                )}
              </>
            )}
          </section>

          {/* Shift schedule */}
          <section className="panel" style={{ marginBottom: 14 }}>
            <div className="panel-head">
              <div>
                <div className="panel-title">Shift schedule</div>
                <div className="panel-note">
                  Recorded shifts and their PagerDuty override stamps
                </div>
              </div>
            </div>
            {shiftsError ? (
              <ErrorState
                title="Unable to load the shift schedule"
                description={shiftsError}
                onRetry={loadShifts}
              />
            ) : shiftsLoading && shifts.length === 0 ? (
              <TableSkeleton rows={6} />
            ) : shifts.length === 0 ? (
              <EmptyState
                title="No shifts recorded"
                description={
                  canManage
                    ? "Record the first on-call shift with the New shift control — the backend attempts the PagerDuty override immediately and stamps the outcome on the row."
                    : "No on-call shifts are on record yet. An Approver records them; this table then shows exactly who a firing alarm pages."
                }
              />
            ) : (
              <div className="table-wrap">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Engineer</th>
                      <th>Role</th>
                      <th>Window</th>
                      <th>PagerDuty sync</th>
                      {canManage && <th style={{ width: 90 }}>Actions</th>}
                    </tr>
                  </thead>
                  <tbody>
                    {shifts.map((shift) => {
                      const stampedError =
                        shift.sync_error &&
                        (isSkippedSyncStatus(shift.sync_status) ||
                          shift.sync_status === "failed")
                          ? shift.sync_error
                          : null;
                      return (
                        <tr key={shift.shift_id}>
                          <td>
                            <span className="doc-name" title={shift.shift_id}>
                              {shift.engineer_name || shift.engineer_email}
                            </span>
                            <div className="doc-sub tnum">
                              {shift.engineer_email}
                              {shift.pagerduty_user_id
                                ? ` · PD ${shift.pagerduty_user_id}`
                                : ""}
                            </div>
                          </td>
                          <td>
                            <span
                              className={`badge ${shift.role === "primary" ? "badge-live" : "badge-neutral"}`}
                              title={
                                shift.role === "primary"
                                  ? "Primary — the alarm pages this engineer first"
                                  : "Secondary — the escalation fallback"
                              }
                            >
                              {shift.role}
                            </span>
                            {liveOverlaps.has(shift.shift_id) && (
                              <div
                                className="doc-sub"
                                style={{
                                  color: "var(--warning)",
                                  marginTop: 3,
                                }}
                                title="This shift's window overlaps another shift in the same role — the newest start_at wins the page"
                              >
                                overlaps another {shift.role} shift
                              </div>
                            )}
                          </td>
                          <td
                            className="tnum"
                            title={`${shift.start_at} → ${shift.end_at} (UTC)`}
                          >
                            {formatShiftRange(shift.start_at, shift.end_at)}
                          </td>
                          <td>
                            <SyncBadge shift={shift} />
                            {stampedError && (
                              <div
                                className="doc-sub"
                                title={stampedError}
                                style={{
                                  color:
                                    shift.sync_status === "failed"
                                      ? "var(--danger)"
                                      : "var(--warning)",
                                  marginTop: 3,
                                }}
                              >
                                {stampedError.length > 60
                                  ? `${stampedError.slice(0, 60)}…`
                                  : stampedError}
                              </div>
                            )}
                            {shift.last_synced_at && (
                              <div
                                className="doc-sub tnum"
                                title={formatDateTime(shift.last_synced_at)}
                              >
                                synced {relativeTime(shift.last_synced_at)}
                              </div>
                            )}
                          </td>
                          {canManage && (
                            <td>
                              <div className="row" style={{ gap: 4 }}>
                                <button
                                  type="button"
                                  className="btn btn-sm btn-ghost"
                                  title="Edit this shift"
                                  aria-label={`Edit shift for ${shift.engineer_email}`}
                                  onClick={() => openEdit(shift)}
                                  disabled={busyId === shift.shift_id}
                                >
                                  <Pencil size={12} />
                                </button>
                                <DeleteShiftButton
                                  shiftId={shift.shift_id}
                                  busy={busyId === shift.shift_id}
                                  onFire={deleteShift}
                                />
                              </div>
                            </td>
                          )}
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
            {shiftsToken && !shiftsError && (
              <div
                className="pager"
                style={{ borderTop: "1px solid var(--border)" }}
              >
                <span className="tnum">
                  {shifts.length} shift{shifts.length === 1 ? "" : "s"} loaded
                </span>
                <div className="row" style={{ gap: 6 }}>
                  <button
                    type="button"
                    className="btn btn-sm"
                    disabled={shiftsLoadingMore}
                    onClick={loadMoreShifts}
                  >
                    {shiftsLoadingMore ? "Loading…" : "Load more"}
                  </button>
                </div>
              </div>
            )}
          </section>

          {/* Admin shift form */}
          {canManage && formOpen && (
            <section className="panel panel-pad" style={{ marginBottom: 14 }}>
              <div
                className="row"
                style={{ justifyContent: "space-between", marginBottom: 12 }}
              >
                <div>
                  <strong style={{ fontSize: 15 }}>
                    {editing ? "Edit shift" : "New shift"}
                  </strong>
                  <div className="panel-note">
                    Recording as {me?.principal} — the backend attempts the
                    PagerDuty override as soon as the shift is saved
                  </div>
                </div>
                <button
                  type="button"
                  className="btn btn-sm btn-ghost"
                  onClick={() => setFormOpen(false)}
                  aria-label="Close the shift form"
                >
                  <X size={12} />
                </button>
              </div>
              {grouped.wholeForm.length > 0 && (
                <div style={{ marginBottom: 10 }}>
                  <InlineWarning title={grouped.wholeForm.join(" · ")} />
                </div>
              )}
              <div
                style={{
                  display: "grid",
                  gap: 12,
                  gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))",
                }}
              >
                <label>
                  <div className="stat-label" style={{ marginBottom: 4 }}>
                    Engineer email
                  </div>
                  <input
                    className="input"
                    style={{ width: "100%" }}
                    value={form.engineer_email}
                    placeholder="oncall@example.com"
                    onChange={(event) =>
                      setForm({ ...form, engineer_email: event.target.value })
                    }
                  />
                  <FieldError errors={grouped.byField.engineer_email} />
                </label>
                <label>
                  <div className="stat-label" style={{ marginBottom: 4 }}>
                    Name (optional)
                  </div>
                  <input
                    className="input"
                    style={{ width: "100%" }}
                    value={form.engineer_name}
                    placeholder="Dana Diaz"
                    onChange={(event) =>
                      setForm({ ...form, engineer_name: event.target.value })
                    }
                  />
                  <FieldError errors={grouped.byField.engineer_name} />
                </label>
                <label>
                  <div className="stat-label" style={{ marginBottom: 4 }}>
                    PagerDuty user id (optional)
                  </div>
                  <input
                    className="input"
                    style={{ width: "100%" }}
                    value={form.pagerduty_user_id}
                    placeholder="PX8Y2KQ"
                    onChange={(event) =>
                      setForm({
                        ...form,
                        pagerduty_user_id: event.target.value,
                      })
                    }
                  />
                  <FieldError errors={grouped.byField.pagerduty_user_id} />
                </label>
                <label>
                  <div className="stat-label" style={{ marginBottom: 4 }}>
                    Role
                  </div>
                  <select
                    className="select"
                    style={{ width: "100%" }}
                    value={form.role}
                    onChange={(event) =>
                      setForm({ ...form, role: event.target.value })
                    }
                  >
                    <option value="primary">Primary</option>
                    <option value="secondary">Secondary</option>
                  </select>
                  <FieldError errors={grouped.byField.role} />
                </label>
                <label>
                  <div className="stat-label" style={{ marginBottom: 4 }}>
                    Starts (your local time)
                  </div>
                  <input
                    type="datetime-local"
                    className="input"
                    style={{ width: "100%" }}
                    value={form.start_at}
                    onChange={(event) =>
                      setForm({ ...form, start_at: event.target.value })
                    }
                  />
                  <FieldError errors={grouped.byField.start_at} />
                </label>
                <label>
                  <div className="stat-label" style={{ marginBottom: 4 }}>
                    Ends (your local time)
                  </div>
                  <input
                    type="datetime-local"
                    className="input"
                    style={{ width: "100%" }}
                    value={form.end_at}
                    onChange={(event) =>
                      setForm({ ...form, end_at: event.target.value })
                    }
                  />
                  <FieldError errors={grouped.byField.end_at} />
                </label>
              </div>
              {formErrors.length === 0 && (
                <p
                  className="panel-note"
                  style={{ marginTop: 10, lineHeight: 1.6 }}
                >
                  Windows are half-open (start inclusive, end exclusive) and can
                  span at most 7 days. A shift without a PagerDuty user id is
                  still recorded — it just cannot reach the schedule, and the
                  table stamps that fact on the row.
                </p>
              )}
              <div className="row" style={{ gap: 8, marginTop: 14 }}>
                <button
                  type="button"
                  className="btn btn-primary"
                  disabled={saving}
                  onClick={saveShift}
                >
                  {saving ? "Saving…" : editing ? "Save changes" : "Create shift"}
                </button>
                <button
                  type="button"
                  className="btn"
                  disabled={saving}
                  onClick={() => setFormOpen(false)}
                >
                  Cancel
                </button>
              </div>
            </section>
          )}

          {/* Paging audit */}
          <section className="panel">
            <div className="panel-head">
              <div>
                <div className="panel-title">Paging audit</div>
                <div className="panel-note">
                  Every alarm notification and where PagerDuty sent it
                </div>
              </div>
              <div className="row" style={{ gap: 8 }}>
                <select
                  className="select"
                  value={alarmFilter}
                  onChange={(event) => pickAlarmFilter(event.target.value)}
                  aria-label="Filter by alarm name"
                  title="Filter the audit by alarm name"
                >
                  <option value="">All alarms</option>
                  {alarmNames.map((name) => (
                    <option key={name} value={name}>
                      {name}
                    </option>
                  ))}
                </select>
                <button
                  type="button"
                  className="btn btn-sm"
                  onClick={loadNotifications}
                  disabled={notifLoading}
                  title="Reload the audit for the selected alarm"
                >
                  <RefreshCw size={12} />
                </button>
              </div>
            </div>
            {notifError ? (
              <ErrorState
                title="Unable to load the paging audit"
                description={notifError}
                onRetry={loadNotifications}
              />
            ) : notifLoading && notifications.length === 0 ? (
              <TableSkeleton rows={6} />
            ) : notifications.length === 0 ? (
              <EmptyState
                title="No alarm notifications on record"
                description={
                  alarmFilter
                    ? `No pages recorded for ${alarmFilter}. Clear the filter to see every alarm.`
                    : "Once a CloudWatch alarm notifies the on-call integration, each page is stamped here with the state, routing and who was on call."
                }
              />
            ) : (
              <div className="table-wrap">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Time</th>
                      <th>Alarm</th>
                      <th>State</th>
                      <th>Routing</th>
                      <th>PagerDuty</th>
                      <th>On call at page time</th>
                    </tr>
                  </thead>
                  <tbody>
                    {notifications.map((notification) => {
                      const severity = pdSeverityForState(
                        notification.state_value,
                      );
                      const onCall = [
                        notification.oncall_primary,
                        notification.oncall_secondary,
                      ]
                        .filter((engineer) => engineer != null)
                        .map(
                          (engineer) =>
                            engineer?.name || engineer?.email || "unknown",
                        );
                      return (
                        <tr key={notification.notification_id}>
                          <td
                            className="tnum"
                            title={formatDateTime(notification.created_at)}
                          >
                            {relativeTime(notification.created_at)}
                          </td>
                          <td>
                            <span
                              className="doc-name"
                              title={
                                notification.alarm_arn ?? notification.alarm_name
                              }
                            >
                              {notification.alarm_name}
                            </span>
                            {notification.summary && (
                              <div className="doc-sub">
                                {notification.summary.length > 80
                                  ? `${notification.summary.slice(0, 80)}…`
                                  : notification.summary}
                              </div>
                            )}
                          </td>
                          <td>
                            <span
                              className="badge badge-neutral"
                              style={{
                                color:
                                  severity === "critical"
                                    ? "var(--danger)"
                                    : severity === "warning"
                                      ? "var(--warning)"
                                      : severity === "error"
                                        ? "var(--danger)"
                                        : "var(--info)",
                              }}
                              title={`PagerDuty severity: ${severity}`}
                            >
                              {notification.event_action ?? "?"}
                              {notification.state_value
                                ? ` · ${notification.state_value}`
                                : ""}
                            </span>
                          </td>
                          <td>
                            <RoutingBadge routing={notification.routing} />
                          </td>
                          <td className="doc-sub tnum">
                            {notification.pd_status ?? "—"}
                            {notification.pd_incident_dedup_key && (
                              <div
                                title={notification.pd_incident_dedup_key}
                                style={{ color: "var(--text-3)" }}
                              >
                                {notification.pd_incident_dedup_key}
                              </div>
                            )}
                          </td>
                          <td>
                            {onCall.length > 0 ? (
                              <span className="doc-sub">{onCall.join(" · ")}</span>
                            ) : (
                              <span className="muted">nobody</span>
                            )}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
            {notifToken && !notifError && (
              <div
                className="pager"
                style={{ borderTop: "1px solid var(--border)" }}
              >
                <span className="tnum">
                  {notifications.length} notification
                  {notifications.length === 1 ? "" : "s"} loaded
                </span>
                <div className="row" style={{ gap: 6 }}>
                  <button
                    type="button"
                    className="btn btn-sm"
                    disabled={notifLoadingMore}
                    onClick={loadMoreNotifications}
                  >
                    {notifLoadingMore ? "Loading…" : "Load more"}
                  </button>
                </div>
              </div>
            )}
          </section>
        </>
      )}
    </div>
  );
}