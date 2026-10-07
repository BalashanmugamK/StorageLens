// Approvals — the write half of the loop. The offline experiments end
// at a recommendation; this page walks each proposal through
// proposed → approved → executed → verified (or failed), and shows the
// realized-savings ledger the verified decisions feed.
//
// Numbers provenance: predicted figures are recomputed by the backend
// with the real engine (bundled byte-identical from optimization/),
// realized figures are those predictions carried by decisions whose S3
// copy was HEAD-verified — never invoice data.

import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  BadgeCheck,
  ShieldAlert,
  WandSparkles,
  X,
} from "lucide-react";
import { api } from "../services/api";
import { useLiveSystem } from "../hooks/useLiveSystem";
import { ReconciliationPanel } from "../components/approvals/ReconciliationPanel";
import {
  EmptyState,
  ErrorState,
  LiveBadge,
  Spinner,
  SignedOutNote,
  StatTile,
  TierDot,
} from "../components/ui/atoms";
import {
  actionsForState,
  isInternalDecisionRow,
  queueForState,
  STATE_LABELS,
  stateColor,
} from "../utils/decisions";
import {
  formatBytes,
  formatDateTime,
  formatMoney,
  formatPercent,
  relativeTime,
  tierLabel,
} from "../utils/format";
import type {
  ApiDecision,
  ApiLedger,
  DecisionState,
} from "../types";

type Tab = "queue" | "approved" | "history" | "ledger" | "reconciliation";

const TABS: { value: Tab; label: string }[] = [
  { value: "queue", label: "Proposed" },
  { value: "approved", label: "Approved" },
  { value: "history", label: "History" },
  { value: "ledger", label: "Ledger" },
  { value: "reconciliation", label: "Reconciliation" },
];

function StateBadge({ state }: { state: DecisionState }) {
  return (
    <span
      className="badge badge-neutral"
      title={STATE_LABELS[state].meaning}
      style={{ color: stateColor(state) }}
    >
      {STATE_LABELS[state].label}
    </span>
  );
}

function TierMove({ from, to }: { from: string; to: string }) {
  return (
    <span className="row tier-move" style={{ gap: 6 }}>
      <span className="row" style={{ gap: 5 }}>
        <TierDot tier={from} />
        <span className="tier-pill">{tierLabel(from)}</span>
      </span>
      <span className="muted">→</span>
      <span
        className="row tier-pill"
        style={{ gap: 5, color: "var(--accent-strong)" }}
      >
        <TierDot tier={to} />
        {tierLabel(to)}
      </span>
    </span>
  );
}

/** Two-step confirm for the irreversible execute action: the first
    click arms the button, the second fires it. */
function ExecuteButton({
  decisionId,
  onDone,
  busy,
}: {
  decisionId: string;
  onDone: (outcome: string) => void;
  busy: boolean;
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
      className={armed ? "btn btn-sm btn-danger" : "btn btn-sm"}
      disabled={busy}
      onClick={async () => {
        if (!armed) {
          setArmed(true);
          return;
        }
        setArmed(false);
        try {
          const result = await api.executeDecision(decisionId);
          onDone(
            result.decision_state === "verified"
              ? `Verified — S3 reports ${tierLabel(
                  result.verified_storage_class ?? "",
                )}; realized ${formatMoney(result.realized_savings ?? 0)}/yr`
              : `Failed: ${result.verification_error ?? "verification error"}`,
          );
        } catch (error) {
          onDone(
            error instanceof Error ? error.message : "Execute failed",
          );
        }
      }}
    >
      {armed ? "Confirm execute" : "Execute"}
    </button>
  );
}

export default function Approvals() {
  const { status } = useLiveSystem();

  const [decisions, setDecisions] = useState<ApiDecision[]>([]);
  const [ledger, setLedger] = useState<ApiLedger | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [proposing, setProposing] = useState(false);
  const [tab, setTab] = useState<Tab>("queue");

  const reload = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const [list, ledger_] = await Promise.all([
        api.listDecisions(),
        api.getLedger(),
      ]);
      setDecisions(list.decisions);
      setLedger(ledger_);
    } catch (error) {
      setLoadError(
        error instanceof Error ? error.message : "Failed to load decisions",
      );
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (status === "online") void reload();
  }, [status, reload]);

  const act = async (
    decisionId: string,
    action: "approve" | "reject",
  ) => {
    setBusyId(decisionId);
    setNotice(null);
    try {
      const result =
        action === "approve"
          ? await api.approveDecision(decisionId)
          : await api.rejectDecision(decisionId);
      setNotice(
        `Decision ${decisionId} ${result.decision_state}.`,
      );
      await reload();
    } catch (error) {
      setNotice(
        error instanceof Error ? error.message : "Action failed",
      );
    } finally {
      setBusyId(null);
    }
  };

  const proposeBatch = async () => {
    setProposing(true);
    setNotice(null);
    try {
      const result = await api.proposeDecisionBatch({ max: 25 });
      const skipped = result.skipped.length;
      setNotice(
        `Proposed ${result.proposed_count} migration${result.proposed_count === 1 ? "" : "s"}` +
          (skipped ? ` · ${skipped} document${skipped === 1 ? "" : "s"} skipped (no migration recommended, already pending, or stale aggregate)` : "") +
          ` — ${result.aggregates_scanned} aggregates scanned.`,
      );
      await reload();
    } catch (error) {
      setNotice(
        error instanceof Error ? error.message : "Propose batch failed",
      );
    } finally {
      setProposing(false);
    }
  };

  const byQueue = useMemo(() => {
    const buckets: Record<Tab, ApiDecision[]> = {
      queue: [],
      approved: [],
      history: [],
      ledger: [],
      reconciliation: [],
    };
    const sorted = [...decisions].sort((a, b) =>
      a.created_at < b.created_at ? 1 : -1,
    );
    for (const decision of sorted) {
      // Guard-marker rows are bookkeeping, not decisions — skip them
      // rather than letting the unknown state land in History.
      if (isInternalDecisionRow(decision)) continue;
      buckets[queueForState(decision.decision_state)].push(decision);
    }
    return buckets;
  }, [decisions]);

  // Verified-only realized figure — the ledger is the backend's
  // source of truth; this tile mirrors its response.
  const verifiedCount = ledger?.entries.length ?? 0;

  const visible =
    tab === "queue"
      ? byQueue.queue
      : tab === "approved"
        ? byQueue.approved
        : tab === "history"
          ? byQueue.history
          : [];

  const pendingCount = byQueue.queue.length;
  const approvedCount = byQueue.approved.length;

  return (
    <div className="page-in">
      <header
        className="page-in"
        style={{
          display: "flex",
          alignItems: "flex-end",
          justifyContent: "space-between",
          gap: 18,
          flexWrap: "wrap",
          marginBottom: 18,
        }}
      >
        <div>
          <div className="row" style={{ gap: 10 }}>
            <h1 style={{ fontSize: 21, letterSpacing: "-0.02em" }}>
              Approvals
            </h1>
            <LiveBadge />
          </div>
          <p style={{ color: "var(--text-2)", marginTop: 4, fontSize: 13.5 }}>
            The loop's write half: approve a proposal, execute the S3
            transition, verify it, and read the realized-savings ledger.
          </p>
        </div>
        <div className="row">
          {status === "online" && (
            <button
              type="button"
              className="btn btn-primary"
              onClick={proposeBatch}
              disabled={proposing || loading}
              title="Run the engine over the latest aggregates and record a proposal for every document it would migrate (up to 25 per click)"
            >
              <WandSparkles size={14} />
              {proposing ? "Proposing…" : "Propose decisions"}
            </button>
          )}
          <button
            type="button"
            className="btn"
            onClick={reload}
            disabled={loading || status === "connecting"}
          >
            Refresh
          </button>
        </div>
      </header>

      {(status === "online" || status === "connecting") &&
        !loadError &&
        (verifiedCount > 0 || (ledger && ledger.total_realized_annual_savings > 0)) && (
          <div
            className="page-in"
            style={{
              display: "grid",
              gap: 12,
              gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))",
              marginBottom: 16,
            }}
          >
            <StatTile
              label="Realized savings / yr (HEAD-verified)"
              value={formatMoney(
                ledger?.total_realized_annual_savings ?? 0,
              )}
              footer={
                <span className="muted" style={{ fontSize: 11 }}>
                  {verifiedCount} verified transition
                  {verifiedCount === 1 ? "" : "s"} · not invoice data
                </span>
              }
            />
            <StatTile
              label="Awaiting approval"
              value={pendingCount}
              footer={
                <span className="muted" style={{ fontSize: 11 }}>
                  recompute with the engine on every proposal
                </span>
              }
            />
            <StatTile
              label="Approved, awaiting execute"
              value={approvedCount}
              footer={
                <span className="muted" style={{ fontSize: 11 }}>
                  execution moves the object in S3
                </span>
              }
            />
          </div>
        )}

      {/* Tab bar */}
      <div className="row" style={{ gap: 6, marginBottom: 12, flexWrap: "wrap" }}>
        {TABS.map((entry) => (
          <button
            key={entry.value}
            type="button"
            className={`btn btn-sm${tab === entry.value ? " btn-primary" : ""}`}
            onClick={() => setTab(entry.value)}
            disabled={entry.value === "ledger" && !ledger}
            title={
              entry.value === "ledger" && !ledger
                ? "Ledger loads with the queue"
                : undefined
            }
          >
            {entry.value === "queue" && pendingCount > 0
              ? `${entry.label} (${pendingCount})`
              : entry.label}
          </button>
        ))}
      </div>

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
          <BadgeCheck size={14} style={{ color: "var(--accent-strong)" }} />
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
            description="Point VITE_API_BASE_URL at your deployed API Gateway endpoint (see frontend/.env.example) to manage approvals."
          />
        </div>
      )}

      {status === "online" && (
        <div className="panel">
          {loadError ? (
            <ErrorState
              description={loadError}
              onRetry={reload}
            />
          ) : loading && decisions.length === 0 ? (
            <div style={{ padding: 16 }}>
              <Spinner />
            </div>
          ) : tab === "ledger" ? (
            <LedgerView ledger={ledger} />
          ) : tab === "reconciliation" ? (
            <ReconciliationPanel />
          ) : visible.length === 0 ? (
            <EmptyState
              title={
                tab === "queue"
                  ? "Nothing awaiting approval"
                  : tab === "approved"
                    ? "No approved decisions pending execution"
                    : "No history yet"
              }
              description={
                tab === "queue"
                  ? "Use Propose decisions (above) — the backend re-runs the engine over the latest aggregates and records every migration it recommends. A single proposal can also be made from a document's detail page."
                  : tab === "approved"
                    ? "Approve a proposal first; execution then copies the object into its approved storage class."
                    : "Rejected, verified and failed decisions appear here once decisions move."
              }
            />
          ) : (
            <div className="table-wrap">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Document</th>
                    <th>Migration</th>
                    <th className="num" title="Predicted annual saving from the engine run recorded at proposal time">
                      Predicted saving / yr
                    </th>
                    <th title="Age of the aggregate the engine forecast consumed">
                      Features as of
                    </th>
                    <th>State</th>
                    <th style={{ width: 210 }}>Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {visible.map((decision) => {
                    const actions = actionsForState(
                      decision.decision_state,
                    );
                    return (
                      <tr key={decision.decision_id}>
                        <td>
                          <Link
                            to={`/documents/${decision.document_id}`}
                            style={{ textDecoration: "none" }}
                          >
                            <span className="doc-name" title={decision.object_key}>
                              {decision.file_name ?? decision.document_id}
                            </span>
                            <div className="doc-sub tnum">
                              {formatBytes(decision.file_size)} ·{" "}
                              {relativeTime(decision.created_at)}
                            </div>
                          </Link>
                        </td>
                        <td>
                          <TierMove
                            from={decision.from_class}
                            to={decision.to_class}
                          />
                          {decision.policy_conflict && (
                            <div
                              className="row"
                              style={{ gap: 5, marginTop: 4, color: "var(--warning)", fontSize: 11 }}
                              title="Policy B kept the current class as cost-safe; executing still moves to the approved target"
                            >
                              <ShieldAlert size={12} /> policy conflict on record
                            </div>
                          )}
                        </td>
                        <td className="num">
                          <span
                            className="tnum"
                            style={{ color: "var(--accent-strong)" }}
                          >
                            {decision.predicted_savings > 0
                              ? formatMoney(decision.predicted_savings)
                              : "—"}
                          </span>
                          {decision.predicted_savings > 0 && (
                            <div className="doc-sub tnum">
                              {formatPercent(decision.savings_percentage, 0)}
                              {decision.to_class === "GLACIER_DEEP_ARCHIVE"
                                ? " · Deep Archive"
                                : ""}
                            </div>
                          )}
                        </td>
                        <td className="tnum" title={formatDateTime(decision.features_as_of)}>
                          {relativeTime(decision.features_as_of)}
                        </td>
                        <td>
                          {tab === "history" ? (
                            <div>
                              <StateBadge state={decision.decision_state} />
                              {decision.decision_state === "verified" && (
                                <div className="doc-sub tnum" style={{ color: "var(--good)" }}>
                                  realized {formatMoney(decision.realized_savings ?? 0)}/yr
                                </div>
                              )}
                              {decision.decision_state === "failed" &&
                                decision.verification_error && (
                                  <div className="doc-sub" title={decision.verification_error}>
                                    {decision.verification_error.slice(0, 60)}
                                    {decision.verification_error.length > 60
                                      ? "…"
                                      : ""}
                                  </div>
                                )}
                            </div>
                          ) : (
                            <StateBadge state={decision.decision_state} />
                          )}
                        </td>
                        <td>
                          <div className="row" style={{ gap: 4 }}>
                            {actions.includes("approve") && (
                              <>
                                <button
                                  type="button"
                                  className="btn btn-sm btn-primary"
                                  disabled={busyId === decision.decision_id}
                                  onClick={() =>
                                    act(decision.decision_id, "approve")
                                  }
                                >
                                  Approve
                                </button>
                                <button
                                  type="button"
                                  className="btn btn-sm btn-ghost"
                                  disabled={busyId === decision.decision_id}
                                  onClick={() =>
                                    act(decision.decision_id, "reject")
                                  }
                                >
                                  Reject
                                </button>
                              </>
                            )}
                            {actions.includes("execute") && (
                              <ExecuteButton
                                decisionId={decision.decision_id}
                                busy={busyId === decision.decision_id}
                                onDone={async (outcome) => {
                                  setNotice(outcome);
                                  await reload();
                                }}
                              />
                            )}
                            {actions.length === 0 && (
                              <span className="muted">—</span>
                            )}
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function LedgerView({ ledger }: { ledger: ApiLedger | null }) {
  if (!ledger) {
    return (
      <EmptyState
        title="Ledger not loaded"
        description="The ledger loads with the approval queue; refresh to retry."
      />
    );
  }

  if (ledger.entries.length === 0) {
    return (
      <EmptyState
        title="No verified transitions yet"
        description="Once a decision executes and its S3 HEAD verification passes, it records realized savings here."
      />
    );
  }

  const sorted = [...ledger.entries].sort((a, b) =>
    (a.verified_at ?? "") < (b.verified_at ?? "") ? 1 : -1,
  );

  return (
    <div style={{ padding: "14px 16px" }}>
      <div
        className="page-in"
        style={{
          display: "grid",
          gap: 12,
          gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))",
          marginBottom: 14,
        }}
      >
        <StatTile
          hero
          label="Total realized / yr"
          value={formatMoney(ledger.total_realized_annual_savings)}
        />
        <StatTile
          label="Verified decisions"
          value={ledger.entries.length}
        />
      </div>

      <table className="data-table">
        <thead>
          <tr>
            <th>Decision</th>
            <th>Document</th>
            <th>Migration</th>
            <th className="num">Realized / yr</th>
            <th>Verified</th>
          </tr>
        </thead>
        <tbody>
          {sorted.map((entry) => (
            <tr key={entry.decision_id}>
              <td>
                <span className="doc-sub tnum">{entry.decision_id}</span>
              </td>
              <td>
                <Link
                  to={`/documents/${entry.document_id}`}
                  style={{ textDecoration: "none" }}
                >
                  <span className="doc-name">
                    {entry.file_name ?? entry.document_id}
                  </span>
                </Link>
              </td>
              <td>
                <TierMove from={entry.from_class} to={entry.to_class} />
              </td>
              <td
                className="num tnum"
                style={{ color: "var(--accent-strong)" }}
              >
                {formatMoney(entry.realized_savings ?? 0)}
              </td>
              <td className="tnum" title={formatDateTime(entry.verified_at)}>
                {relativeTime(entry.verified_at)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <p
        className="muted"
        style={{ fontSize: 11.5, margin: "12px 0 4px", lineHeight: 1.6 }}
      >
        Basis: {ledger.basis}. Each figure is the predicted annual saving
        recorded at proposal time (features as of{" "}
        {relativeTime(sorted[0]?.features_as_of)} for the most recent),
        carried by a transition whose object now HEADs at the approved
        storage class — treat this panel as a conservative floor rather
        than an invoice.
      </p>
    </div>
  );
}