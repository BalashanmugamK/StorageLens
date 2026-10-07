// Document detail — live document record and access statistics from
// the API; recommendation, candidate-tier costs and any policy
// conflict from the experiment join. Provenance is labeled everywhere.

import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  AlertTriangle,
  ArrowLeft,
  Clock,
  ClipboardCheck,
  History,
  Activity,
} from "lucide-react";
import { api } from "../services/api";
import { useLiveSystem } from "../hooks/useLiveSystem";
import {
  toJoinedRecommendation,
  useExperimentJoin,
} from "../hooks/useExperimentJoin";
import type { ApiAccessStats, ApiDocument } from "../types";
import { DownloadButton } from "../components/documents/DownloadButton";
import { BarList } from "../components/charts/BarList";
import {
  ErrorState,
  ExperimentBadge,
  LiveBadge,
  Spinner,
  StatTile,
  TierDot,
} from "../components/ui/atoms";
import {
  fileExtension,
  formatBytes,
  formatDateTime,
  formatMoney,
  formatPercent,
  tierLabel,
} from "../utils/format";
import { PRICING_REGION } from "../utils/pricing";

function PolicyConflictCard({
  currentTier,
  documentState,
  costSafeTier,
  premium,
}: {
  currentTier: string;
  documentState: string;
  costSafeTier: string;
  premium: number | null;
}) {
  return (
    <div
      className="mt-3"
      role="note"
      style={{
        border: "1px solid var(--warning-border)",
        background: "var(--warning-dim)",
        borderRadius: "var(--radius)",
        padding: "16px 18px",
      }}
    >
      <div className="row" style={{ gap: 9, color: "var(--warning)" }}>
        <AlertTriangle size={16} />
        <strong style={{ fontSize: 13.5 }}>Policy conflict</strong>
        <span
          className="badge badge-warning"
          style={{ marginLeft: "auto" }}
        >
          Compliance premium {premium !== null ? `${formatMoney(premium)}/yr` : "unmodeled"}
        </span>
      </div>
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))",
          gap: 12,
          marginTop: 12,
          fontSize: 12.5,
        }}
      >
        <div>
          <div className="stat-label">Current tier</div>
          <div style={{ color: "var(--text)", fontWeight: 550, marginTop: 2 }}>
            {tierLabel(currentTier)}
          </div>
        </div>
        <div>
          <div className="stat-label">Configured state</div>
          <div style={{ color: "var(--text)", fontWeight: 550, marginTop: 2 }}>
            {documentState}
          </div>
        </div>
        <div>
          <div className="stat-label">Cost-safe recommendation</div>
          <div style={{ color: "var(--text)", fontWeight: 550, marginTop: 2 }}>
            {tierLabel(costSafeTier)}
          </div>
        </div>
      </div>
      <p style={{ fontSize: 12.5, color: "var(--text-2)", marginTop: 12, lineHeight: 1.6 }}>
        The document's current storage class is not eligible for its configured
        state. Under Policy B the optimizer keeps the document where it is —
        moving it to an eligible tier would increase the modeled 12-month cost
        or violate the state constraint — and reports the conflict instead of
        forcing a migration.
      </p>
    </div>
  );
}

export default function DocumentDetail() {
  const { documentId } = useParams<{ documentId: string }>();
  const { documents, status } = useLiveSystem();
  const experimentJoin = useExperimentJoin();

  const [document, setDocument] = useState<ApiDocument | null>(null);
  const [stats, setStats] = useState<ApiAccessStats | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [proposeBusy, setProposeBusy] = useState(false);
  // Outcome of the propose action: a decision was proposed, the
  // backend refused (no aggregate / keep-in-place / already open), or
  // the request failed. The message is shown inline, never as a
  // silent no-op.
  const [proposeResult, setProposeResult] = useState<
    { ok: boolean; message: string } | null
  >(null);

  const load = useCallback(async () => {
    if (!documentId) return;
    setLoading(true);
    setError(null);
    try {
      const [doc, accessStats] = await Promise.all([
        documents.find((d) => d.document_id === documentId) ?? api.getDocument(documentId),
        api.getAccessStats(documentId),
      ]);
      setDocument(doc);
      setStats(accessStats);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  }, [documentId, documents]);

  useEffect(() => {
    load();
  }, [load]);

  const joined = document && experimentJoin?.[documentId ?? ""];
  const rec = joined ? toJoinedRecommendation(joined) : null;

  // Propose a migration: the backend re-runs the REAL engine over the
  // document's latest aggregate and records the decision — unlike the
  // experiment join above, which is an offline artifact.
  const proposeMigration = useCallback(async () => {
    if (!documentId) return;
    setProposeBusy(true);
    setProposeResult(null);
    try {
      const decision = await api.createDecision(documentId);
      setProposeResult({
        ok: true,
        message: `Proposal recorded — ${tierLabel(
          decision.from_class,
        )} → ${tierLabel(decision.to_class)}, ${formatMoney(
          decision.predicted_savings,
        )}/yr predicted. Open the Approvals queue to review it.`,
      });
    } catch (err) {
      setProposeResult({
        ok: false,
        message:
          err instanceof Error
            ? err.message
            : "Failed to record the proposal",
      });
    } finally {
      setProposeBusy(false);
    }
  }, [documentId]);

  if (error) {
    return (
      <div className="page-in">
        <div className="panel">
          <ErrorState
            title="Document unavailable"
            description={error}
            onRetry={load}
          />
        </div>
      </div>
    );
  }

  if (loading || !document) {
    return (
      <div className="page-in" aria-hidden="true">
        <div className="skeleton" style={{ height: 120, borderRadius: 14, marginBottom: 16 }} />
        <div className="grid-2">
          <div className="skeleton" style={{ height: 200, borderRadius: 14 }} />
          <div className="skeleton" style={{ height: 200, borderRadius: 14 }} />
        </div>
      </div>
    );
  }

  // Candidate tier costs from the experiment record (real corpora carry
  // the full modeled set; synthetic carries none).
  const tierCostEntries = rec?.tierCosts
    ? Object.entries(rec.tierCosts)
        .map(([tier, cost]) => ({ tier, cost }))
        .sort((a, b) => a.cost - b.cost)
    : [];

  return (
    <div className="page-in">
      <header style={{ marginBottom: 20 }}>
        <Link
          to="/documents"
          className="row"
          style={{ color: "var(--text-2)", fontSize: 13, textDecoration: "none", gap: 6 }}
        >
          <ArrowLeft size={14} /> Documents
        </Link>
        <div
          className="row"
          style={{ gap: 12, flexWrap: "wrap", marginTop: 10, justifyContent: "space-between" }}
        >
          <div className="row" style={{ gap: 12 }}>
            <span
              className="doc-ico"
              style={{ width: 44, height: 44, fontSize: 10, fontWeight: 700, borderRadius: 10 }}
            >
              {fileExtension(document.file_name)}
            </span>
            <div>
              <h1 style={{ fontSize: 19, letterSpacing: "-0.01em" }}>{document.file_name}</h1>
              <div className="row" style={{ gap: 8, marginTop: 2 }}>
                <LiveBadge />
                <span className="muted mono">{document.content_type}</span>
              </div>
            </div>
          </div>
          <div className="row" style={{ gap: 8 }}>
            <DownloadButton documentId={document.document_id} small={false} />
            <button
              type="button"
              className="btn"
              disabled={proposeBusy || status !== "online"}
              title={
                status === "online"
                  ? "Re-run the real engine against this document's latest aggregate and record an approval-ready decision"
                  : "Proposing needs a reachable, signed-in backend session"
              }
              onClick={proposeMigration}
            >
              <ClipboardCheck size={14} />
              {proposeBusy ? "Proposing…" : "Propose migration"}
            </button>
          </div>
        </div>

        {proposeResult && (
          <div
            className="row"
            role="status"
            style={{
              marginTop: 10,
              gap: 8,
              border: `1px solid ${
                proposeResult.ok
                  ? "var(--border-strong)"
                  : "var(--warning-border)"
              }`,
              background: proposeResult.ok
                ? "transparent"
                : "var(--warning-dim)",
              borderRadius: "var(--radius-sm)",
              padding: "8px 12px",
              fontSize: 12.5,
              color: "var(--text-2)",
            }}
          >
            {proposeResult.message}
            {proposeResult.ok && (
              <Link
                to="/approvals"
                className="btn btn-sm"
                style={{ marginLeft: "auto" }}
              >
                Open Approvals
              </Link>
            )}
          </div>
        )}
      </header>

      {rec?.policyConflict && (
        <PolicyConflictCard
          currentTier={document.current_storage_class}
          documentState={rec.documentState ?? "unknown"}
          costSafeTier={
            // Policy B's cost-safe move: keep the current tier when the
            // current cost is cheapest among candidates, else the cheapest
            // eligible tier whose modeled cost beats it.
            rec.tierCosts &&
            rec.tierCosts[document.current_storage_class] !== undefined &&
            rec.tierCosts[document.current_storage_class] <=
              Math.min(...Object.values(rec.tierCosts))
              ? document.current_storage_class
              : rec.recommendedTier
          }
          premium={rec.savings < 0 ? -rec.savings : null}
        />
      )}

      <div
        className="grid-2 mt-3"
        style={{ gridTemplateColumns: "repeat(auto-fit, minmax(300px, 1fr))" }}
      >
        {/* Live storage record */}
        <section className="panel">
          <div className="panel-head">
            <div className="panel-title">Storage</div>
            <LiveBadge />
          </div>
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))",
              gap: 10,
              padding: "14px 22px 18px",
            }}
          >
            <StatTile label="Object size" value={<span className="tnum">{formatBytes(document.file_size)}</span>} />
            <StatTile
              label="Storage tier"
              value={
                <span className="row" style={{ gap: 8 }}>
                  <TierDot tier={document.current_storage_class} />
                  {tierLabel(document.current_storage_class)}
                </span>
              }
            />
            {document.document_state && (
              <StatTile label="Document state" value={document.document_state} />
            )}
            <StatTile label="Uploaded" value={<span className="tnum" style={{ fontSize: 15 }}>{formatDateTime(document.upload_timestamp)}</span>} />
            {document.object_key && (
              <div className="mono muted" style={{ gridColumn: "1 / -1", wordBreak: "break-all" }}>
                {document.object_key}
              </div>
            )}
          </div>
        </section>

        {/* Live access stats */}
        <section className="panel">
          <div className="panel-head">
            <div className="panel-title">Access statistics</div>
            <LiveBadge />
          </div>
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))",
              gap: 10,
              padding: "14px 22px 18px",
            }}
          >
            <StatTile
              label="All-time accesses"
              value={<span className="tnum">{stats ? stats.access_count : "—"}</span>}
              footer={
                <div className="row muted" style={{ fontSize: 11, gap: 5 }}>
                  <History size={12} /> downloads on record
                </div>
              }
            />
            <StatTile
              label="Last 30 days"
              value={<span className="tnum">{stats ? stats.recent_access_count : "—"}</span>}
              footer={
                <div className="row muted" style={{ fontSize: 11, gap: 5 }}>
                  <Activity size={12} /> aggregation window
                </div>
              }
            />
            <StatTile
              label="Last access"
              value={<span className="tnum" style={{ fontSize: 15 }}>{stats ? daysSinceLastAccessRelative(stats.last_accessed) : "—"}</span>}
              footer={
                <div className="row muted" style={{ fontSize: 11, gap: 5 }}>
                  <Clock size={12} /> {formatDateTime(stats?.last_accessed)}
                </div>
              }
            />
          </div>
          <p className="panel-note" style={{ padding: "0 22px 16px", lineHeight: 1.6 }}>
            Individual access events are not exposed by the API, so no event
            timeline is fabricated — the counts above are the authoritative
            live statistics.
          </p>
        </section>
      </div>

      {/* Experiment recommendation */}
      <section className="panel mt-3">
        <div className="panel-head">
          <div>
            <div className="panel-title">Modeled recommendation</div>
            <div className="panel-note">Offline optimizer run · pricing snapshot {PRICING_REGION}</div>
          </div>
          <ExperimentBadge />
        </div>

        {loading && <div className="row mt-2" style={{ padding: "0 22px" }}><Spinner size={14} /></div>}

        {rec ? (
          <>
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))",
                gap: 10,
                padding: "14px 22px 4px",
              }}
            >
              <StatTile
                label="Current storage (live)"
                value={
                  <span className="row" style={{ gap: 8 }}>
                    <TierDot tier={document.current_storage_class} />
                    <span style={{ fontSize: 15 }}>{tierLabel(document.current_storage_class)}</span>
                  </span>
                }
                footer={<span className="tnum muted" style={{ fontSize: 11.5 }}>{formatBytes(document.file_size)} · {tierLabel(document.current_storage_class)} rate</span>}
              />
              <StatTile
                label="Recommended (experiment)"
                value={
                  <span className="row" style={{ gap: 8 }}>
                    <TierDot tier={rec.recommendedTier} />
                    <span style={{ fontSize: 15 }}>{tierLabel(rec.recommendedTier)}</span>
                  </span>
                }
                footer={<span style={{ fontSize: 11.5, color: "var(--text-2)" }}>cheapest eligible class</span>}
              />
              <StatTile
                label="Modeled savings"
                value={
                  <span
                    className="tnum"
                    style={{
                      fontSize: 26,
                      color:
                        rec.savings > 0
                          ? "var(--accent-strong)"
                          : rec.savings < 0
                            ? "var(--danger)"
                            : "var(--text-2)",
                    }}
                  >
                    {rec.savings > 0
                      ? `${formatMoney(rec.savings)} / yr`
                      : rec.savings < 0
                        ? `−${formatMoney(-rec.savings)} / yr`
                        : "no change"}
                  </span>
                }
                footer={
                  <span className="tnum muted" style={{ fontSize: 11.5 }}>
                    {rec.savings !== 0 && formatPercent(rec.savingsPercentage)} vs current tier
                  </span>
                }
              />
              <StatTile label="Verdict" value={<span style={{ fontSize: 15 }}>{rec.verdict}</span>} />
            </div>

            {tierCostEntries.length > 0 && (
              <div className="mt-2">
                <div style={{ padding: "0 22px" }} className="section-label">
                  Modeled annual cost by candidate tier
                </div>
                <BarList
                  entries={tierCostEntries.map((e) => ({
                    label: tierLabel(e.tier),
                    series: [
                      {
                        value: e.cost,
                        color:
                          e.tier === rec.recommendedTier
                            ? "var(--series-optimized)"
                            : e.tier === document.current_storage_class
                              ? "var(--series-current)"
                              : "var(--panel-3)",
                        name:
                          e.tier === rec.recommendedTier
                            ? "Recommended"
                            : e.tier === document.current_storage_class
                              ? "Current tier"
                              : "Candidate",
                      },
                    ],
                  }))}
                  formatValue={(v) => formatMoney(v, { precision: 6 })}
                  ariaLabel="Modeled annual cost for each candidate storage tier"
                  tableCaption="Modeled 12-month cost per eligible tier for this document (experiment artifact)."
                />
                <p className="panel-note" style={{ padding: "0 22px 16px" }}>
                  <span className="legend-swatch" style={{ background: "var(--series-optimized)", display: "inline-block" }} /> recommended
                  {"   "}
                  <span className="legend-swatch" style={{ background: "var(--series-current)", display: "inline-block" }} /> current
                  {"   "}
                  <span className="legend-swatch" style={{ background: "var(--panel-3)", display: "inline-block", border: "1px solid var(--border)" }} /> candidate
                </p>
              </div>
            )}
          </>
        ) : (
          <div style={{ padding: "10px 22px 20px" }}>
            <div className="skeleton" style={{ height: 90, borderRadius: 10 }} />
            <p className="panel-note mt-2" style={{ lineHeight: 1.6 }}>
              {experimentJoin
                ? "No experiment artifact covers this document id — run an optimizer experiment to model it."
                : "Loading experiment index…"}
            </p>
          </div>
        )}
      </section>
    </div>
  );
}

function daysSinceLastAccessRelative(timestamp: string | null): string {
  if (!timestamp) return "never";
  const ms = Date.now() - Date.parse(timestamp);
  if (Number.isNaN(ms)) return "never";
  const days = ms / 86400000;
  return days < 1 ? `${Math.max(1, Math.round(ms / 3600000))} h ago` : `${days.toFixed(days < 3 ? 1 : 0)} d ago`;
}