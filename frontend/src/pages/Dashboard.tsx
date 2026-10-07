// Dashboard — answers "what is happening with my storage costs?"
// Live system data (document counts, API status, aggregation runs)
// is always labeled LIVE; every dollar figure comes from a bundled
// experiment artifact and is labeled EXPERIMENT. The two are never
// mixed in the same number.

import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import {
  ArrowRight,
  Check,
  Database,
  FlaskConical,
  HardDrive,
  Play,
} from "lucide-react";
import type { ApiAggregationStats, ExperimentBundle } from "../types";
import { TIER_ORDER } from "../types";
import { loadExperiments } from "../data/experiments";
import { useLiveSystem } from "../hooks/useLiveSystem";
import { api } from "../services/api";
import {
  EmptyState,
  ErrorState,
  ExperimentBadge,
  LiveBadge,
  Spinner,
  tierIndex,
} from "../components/ui/atoms";
import { BarList } from "../components/charts/BarList";
import { StackedTierBar } from "../components/charts/StackedTierBar";
import type { TierSegment } from "../components/charts/StackedTierBar";
import { DeltaColumns } from "../components/charts/WaterfallChart";
import type { DeltaEntry } from "../components/charts/WaterfallChart";
import { LiveCostSnapshot } from "../components/dashboard/LiveCostSnapshot";
import {
  formatBytes,
  formatCount,
  formatGb,
  formatMoney,
  formatMoneyCompact,
  formatPercent,
  formatSeconds,
  tierLabel,
} from "../utils/format";
import { PRICING_REGION } from "../utils/pricing";

function tierSegments(
  distribution: Record<string, { document_count: number; storage_gb?: number }>,
  annualCosts?: Record<string, number>,
): TierSegment[] {
  return TIER_ORDER.filter(
    (tier) => distribution[tier]?.document_count,
  ).map((tier) => ({
    tier,
    tierLabel: tierLabel(tier),
    color: `var(--tier-${tierIndex(tier) + 1})`,
    documentCount: distribution[tier].document_count,
    storageGb: distribution[tier].storage_gb,
    annualCost: annualCosts?.[tier],
  }));
}

function AggregationPanel({
  benchmark,
}: {
  benchmark: ExperimentBundle["aggregation_benchmark"];
}) {
  const {
    status,
    documents,
    error,
    lastAggregation,
    refreshDocuments,
    recordAggregation,
  } = useLiveSystem();
  const [running, setRunning] = useState(false);
  const [runError, setRunError] = useState<string | null>(null);
  const [runResult, setRunResult] = useState<number | null>(null);
  // Live pipeline state (GET /aggregates/stats): aggregates-table
  // coverage and the server-side last-run stamp. The localStorage
  // record stays as the per-browser line — it says so explicitly.
  const [liveStats, setLiveStats] = useState<ApiAggregationStats | null>(null);

  const refreshStats = () => {
    if (status !== "online") return;
    api
      .getAggregationStats()
      .then(setLiveStats)
      .catch(() => {
        // Keep the panel running on document counts alone.
      });
  };

  // Re-poll whenever the live connection comes up, and again after
  // each run below.
  useEffect(refreshStats, [status]);

  const runAggregation = async () => {
    setRunning(true);
    setRunError(null);
    setRunResult(null);
    try {
      const result = await api.runAggregation();
      setRunResult(result.documents_processed);
      recordAggregation(result.documents_processed);
      refreshStats();
    } catch (err) {
      setRunError((err as Error).message);
    } finally {
      setRunning(false);
    }
  };

  return (
    <section className="panel chart-card">
      <div className="panel-head">
        <div>
          <div className="panel-title">Aggregate 30-day access history</div>
          <div className="panel-note">
            Folds each document's recent DOWNLOAD events into access
            features and writes the snapshot the optimizer consumes
          </div>
        </div>
        <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
          <button
            className="btn btn-sm btn-ghost"
            onClick={refreshDocuments}
            disabled={status === "connecting"}
          >
            Refresh documents
          </button>
          <button
            className="btn btn-sm btn-primary"
            onClick={runAggregation}
            disabled={running || status !== "online"}
            title={
              status === "online"
                ? undefined
                : "Aggregation needs a reachable backend API"
            }
          >
            {running ? <Spinner size={14} /> : <Play size={14} />}
            {running ? "Analyzing…" : "Run Analysis"}
          </button>
          <LiveBadge />
        </div>
      </div>

      <div style={{ padding: "16px 22px 20px" }}>
      {status !== "online" && !running && (
        <p
          style={{
            fontSize: 12.5,
            color: "var(--text-3)",
            marginTop: 12,
          }}
        >
          {status === "unconfigured"
            ? "No API base URL configured — see Settings."
            : status === "connecting"
              ? "Connecting to the backend…"
              : `API unreachable: ${error ?? "unknown error"}`}
        </p>
      )}

      {running && (
        <div className="progress mt-3" role="status">
          <div className="progress-bar indeterminate" />
        </div>
      )}

      {runResult !== null && !running && (
        <div
          className="row mt-3"
          role="status"
          style={{
            border: "1px solid var(--accent-border)",
            background: "var(--accent-dim)",
            borderRadius: "var(--radius-sm)",
            padding: "10px 14px",
            fontSize: 13,
            color: "var(--accent-strong)",
          }}
        >
          <Check size={15} />
          <span>
            Analysis complete — <strong className="tnum">{formatCount(runResult)}</strong>{" "}
            documents processed
          </span>
        </div>
      )}

      {runError && (
        <p style={{ fontSize: 12.5, color: "var(--danger)", marginTop: 12 }}>
          {runError}
        </p>
      )}

      {(status === "online" || documents.length > 0) && (
        <div
          className="row mt-3"
          style={{ justifyContent: "space-between", flexWrap: "wrap", gap: 10 }}
        >
          <div className="row" style={{ gap: 18, fontSize: 12.5, color: "var(--text-2)", flexWrap: "wrap" }}>
            <span className="row" style={{ gap: 7 }}>
              <Database size={14} className="muted" />
              <strong className="tnum" style={{ color: "var(--text)" }}>
                {formatCount(documents.length)}
              </strong>{" "}
              documents (live)
            </span>
            {liveStats && (
              <span className="row" style={{ gap: 7 }}>
                <HardDrive size={14} className="muted" />
                <strong className="tnum" style={{ color: "var(--text)" }}>
                  {formatCount(liveStats.aggregates)}
                </strong>{" "}
                aggregates
                {liveStats.missing_aggregates > 0 ? (
                  <>
                    {" "}
                    ·{" "}
                    <strong className="tnum" style={{ color: "var(--warning)" }}>
                      {formatCount(liveStats.missing_aggregates)}
                    </strong>{" "}
                    missing
                  </>
                ) : (
                  " · full coverage"
                )}
              </span>
            )}
            <span className="row" style={{ gap: 7 }}>
              <HardDrive size={14} className="muted" />
              <strong className="tnum" style={{ color: "var(--text)" }}>
                {formatBytes(documents.reduce((sum, d) => sum + (d.file_size || 0), 0))}
              </strong>{" "}
              metadata footprint
            </span>
          </div>
          <Link to="/documents" className="badge badge-live" style={{ textDecoration: "none" }}>
            Browse live documents <ArrowRight size={12} />
          </Link>
        </div>
      )}

      {liveStats?.last_aggregation_timestamp && (
        <p className="mt-1" style={{ fontSize: 11.5, color: "var(--text-3)" }}>
          Last pipeline run completed{" "}
          {new Date(liveStats.last_aggregation_timestamp).toLocaleString()} —
          live from the aggregates table.
        </p>
      )}
      {lastAggregation && (
        <p className="mt-1" style={{ fontSize: 11.5, color: "var(--text-3)" }}>
          Last run from this browser processed{" "}
          {formatCount(lastAggregation.documentsProcessed)} documents.
        </p>
      )}
      </div>

      <BenchmarkBlock benchmark={benchmark} />
    </section>
  );
}

/** Measured aggregation-pipeline performance from the offline
 * benchmark artifact — an EXPERIMENT read shown beside the live
 * run panel so the pipeline's own character is visible without
 * any live/experiment data mixing. */
function BenchmarkBlock({
  benchmark,
}: {
  benchmark: ExperimentBundle["aggregation_benchmark"];
}) {
  const largest = Math.max(...benchmark.runs.map((r) => r.documents), 0);
  const seq = benchmark.runs.find((r) => r.documents === largest && r.pattern === "sequential");
  const par = benchmark.runs.find((r) => r.documents === largest && r.pattern === "parallel");
  const speedup =
    seq && par && par.seconds > 0 ? seq.seconds / par.seconds : null;

  return (
    <div style={{ borderTop: "1px solid var(--border)", padding: "16px 22px 20px" }}>
      <div className="row" style={{ justifyContent: "space-between", flexWrap: "wrap", gap: 10 }}>
        <div className="section-label" style={{ padding: 0 }}>
          Benchmarked pipeline performance
        </div>
        <ExperimentBadge note="Offline-measured aggregation benchmark artifact — not a live measurement" />
      </div>
      <div className="table-wrap" style={{ marginTop: 10 }}>
        <table className="data-table" style={{ fontSize: 12.5 }}>
          <caption className="muted" style={{ textAlign: "left", padding: "0 2px 8px", fontSize: 12 }}>
            Access aggregation wall-time by pattern and corpus size
            ({benchmark.region}).
          </caption>
          <thead>
            <tr>
              <th>Pattern</th>
              <th>Docs</th>
              <th>Duration</th>
              <th>Ms / document</th>
              <th>Access events</th>
            </tr>
          </thead>
          <tbody>
            {benchmark.runs.map((run, i) => (
              <tr key={i}>
                <td>{run.pattern}</td>
                <td className="num">{formatCount(run.documents)}</td>
                <td className="num">{formatSeconds(run.seconds)}</td>
                <td className="num tnum">{run.ms_per_document.toFixed(2)}</td>
                <td className="num">{formatCount(run.aggregated_access_events)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {speedup !== null && (
        <p className="panel-note" style={{ marginTop: 8, lineHeight: 1.6 }}>
          On {formatCount(largest)} documents, the parallel pattern finished in{" "}
          {formatSeconds(par ? par.seconds : 0)} vs{" "}
          {formatSeconds(seq ? seq.seconds : 0)} sequential — a{" "}
          <strong className="tnum">{speedup.toFixed(1)}×</strong> speedup.
          Benchmark corpus: {formatCount(benchmark.documents_available)}{" "}
          documents available, measured over two passes per configuration.
        </p>
      )}
    </div>
  );
}

/** Savings-by-component panel — the optimizer's per-component cost
 * changes (optimizer − baseline), drawn as normal anchored bars:
 * savings below the zero line, added cost above. EXPERIMENT data
 * throughout; if the component deltas don't sum to the total change
 * (dataset drift), a neutral reconciliation bar is appended rather
 * than rendering bars that don't add up. */
function SavingsChangePanel({
  summary,
  ariaLabel,
}: {
  summary: ExperimentBundle["synthetic"]["summary"];
  ariaLabel: string;
}) {
  const baseline = summary.baseline_cost_breakdown;
  const optimized = summary.optimizer_cost_breakdown;

  return useMemo(() => {
    const components = [
      { key: "retrieval_cost", label: "Retrieval" },
      { key: "storage_cost", label: "Storage" },
      { key: "request_cost", label: "Requests" },
      { key: "transition_cost", label: "Transitions" },
    ] as const;

    // The delta values are cost CHANGES (optimizer − baseline); the
    // residual is whatever they miss the total change by.
    const deltas: DeltaEntry[] = components.map(({ key, label }) => ({
      label,
      value: optimized[key] - baseline[key],
      note: (
        <div style={{ marginTop: 2, color: "var(--text-3)" }}>
          {formatMoneyCompact(baseline[key])} → {formatMoneyCompact(optimized[key])} per year
        </div>
      ),
    }));

    const residual =
      summary.baseline_cost + deltas.reduce((sum, d) => sum + d.value, 0) -
      summary.optimizer_cost;

    const entries: DeltaEntry[] = [
      ...deltas,
      ...(Math.abs(residual) > 0.5
        ? ([
            {
              label: "Residual",
              value: residual,
              neutral: true,
              note: (
                <div style={{ marginTop: 2, color: "var(--text-3)" }}>
                  Component changes don't fully explain the total change —
                  shown as a reconciliation bar.
                </div>
              ),
            },
          ] satisfies DeltaEntry[])
        : []),
    ];

    return (
      <DeltaColumns
        deltas={entries}
        baselineTotal={summary.baseline_cost}
        formatValue={formatMoneyCompact}
        ariaLabel={ariaLabel}
        tableCaption="Modeled per-component annual cost changes from the baseline allocation to the optimizer recommendation (synthetic workload, 12-month horizon)."
      />
    );
  }, [summary, baseline, optimized, ariaLabel]);
}

/** Side-by-side read of the two modeled corpora. Both columns are
 * EXPERIMENT data sourced from the bundle; no cell is fabricated —
 * where a value has no counterpart in the other corpus, that is
 * stated instead of invented. */
function CorpusComparisonPanel({ bundle }: { bundle: ExperimentBundle }) {
  const real = bundle.real;
  const synth = bundle.synthetic;

  const rows: { metric: string; real: ReactNode; synthetic: ReactNode }[] = [
    {
      metric: "Documents",
      real: formatCount(real.corpus.document_count),
      synthetic: formatCount(synth.summary.total_documents),
    },
    {
      metric: "Storage",
      real: formatBytes(real.corpus.total_bytes),
      synthetic: formatGb(synth.summary.total_storage_gb),
    },
    {
      metric: "Provenance",
      real: (
        <>
          {real.corpus.source}
          <span className="muted"> · {real.corpus.year.join(", ")}</span>
        </>
      ),
      synthetic: (
        <>
          {synth.experiment.workload_file}
          <span className="muted"> · modeled forecast</span>
        </>
      ),
    },
    {
      metric: "Access events",
      real: `${formatCount(real.header.access_events_on_record)} on record`,
      synthetic: "Modeled forecast — no observed access log",
    },
    {
      metric: "Modeled annual savings",
      real: (
        <>
          <strong className="tnum">{formatMoney(real.header.projected_savings)}</strong>{" "}
          <span className="muted">({formatPercent(real.header.savings_percentage, 1)})</span>
        </>
      ),
      synthetic: (
        <>
          <strong className="tnum">{formatMoney(synth.summary.optimizer_savings_vs_baseline)}</strong>{" "}
          <span className="muted">({formatPercent(synth.summary.optimizer_savings_percentage_vs_baseline, 1)})</span>
        </>
      ),
    },
  ];

  return (
    <section className="panel chart-card">
      <div className="panel-head">
        <div>
          <div className="panel-title">What the two corpora agree on</div>
          <div className="panel-note">
            Real judicial corpus vs synthetic workload — identical optimizer,
            different documents
          </div>
        </div>
        <ExperimentBadge />
      </div>
      <div className="table-wrap" style={{ padding: "16px 22px 4px" }}>
        <table className="data-table" style={{ fontSize: 12.5 }}>
          <thead>
            <tr>
              <th>Metric</th>
              <th>Real judicial corpus</th>
              <th>Synthetic workload</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.metric}>
                <td style={{ color: "var(--text-2)" }}>{row.metric}</td>
                <td className="num">{row.real}</td>
                <td className="num">{row.synthetic}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="panel-note" style={{ padding: "10px 22px 18px", lineHeight: 1.6 }}>
        The diversified 500-document corpus (five public collections) lands
        between the two:{" "}
        <strong className="tnum">
          {formatPercent(bundle.diversified.header.savings_percentage, 1)}
        </strong>{" "}
        modeled annual savings. Full runs and methodology live in{" "}
        <Link to="/experiments">Experiments</Link>.
      </p>
    </section>
  );
}

export default function Dashboard() {
  const [bundle, setBundle] = useState<ExperimentBundle | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    loadExperiments()
      .then((data) => {
        if (!cancelled) setBundle(data);
      })
      .catch(() => {
        if (!cancelled) setLoadError("Experiment artifacts failed to load.");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (loadError) {
    return <ErrorState title="Experiment data unavailable" description={loadError} />;
  }

  if (!bundle) {
    return (
      <div aria-hidden="true">
        <div className="skeleton" style={{ height: 240, marginBottom: 18, borderRadius: 14 }} />
        <div className="grid-2" style={{ marginBottom: 18 }}>
          <div className="skeleton" style={{ height: 260, borderRadius: 14 }} />
          <div className="skeleton" style={{ height: 260, borderRadius: 14 }} />
        </div>
        <div className="skeleton" style={{ height: 200, borderRadius: 14 }} />
      </div>
    );
  }

  const summary = bundle.synthetic.summary;
  const breakdown = summary.optimizer_cost_breakdown;
  const baselineBreakdown = summary.baseline_cost_breakdown;

  const costEntries = [
    {
      label: "Retrieval",
      series: [
        { value: baselineBreakdown.retrieval_cost, color: "var(--series-current)", name: "Baseline" },
        { value: breakdown.retrieval_cost, color: "var(--series-optimized)", name: "Optimizer" },
      ],
      callout: (
        <span>
          (
          {formatPercent(
            100 * (1 - breakdown.retrieval_cost / baselineBreakdown.retrieval_cost),
            0,
          )}{" "}
          reduction)
        </span>
      ),
    },
    {
      label: "Storage",
      series: [
        { value: baselineBreakdown.storage_cost, color: "var(--series-current)", name: "Baseline" },
        { value: breakdown.storage_cost, color: "var(--series-optimized)", name: "Optimizer" },
      ],
      callout: (
        <span>
          (+{formatPercent(100 * (breakdown.storage_cost / baselineBreakdown.storage_cost - 1), 0)}{" "}
          — safer, cheaper tiers)
        </span>
      ),
    },
    {
      label: "Requests",
      series: [
        { value: baselineBreakdown.request_cost, color: "var(--series-current)", name: "Baseline" },
        { value: breakdown.request_cost, color: "var(--series-optimized)", name: "Optimizer" },
      ],
    },
    {
      label: "Transitions",
      series: [
        { value: baselineBreakdown.transition_cost, color: "var(--series-current)", name: "Baseline" },
        { value: breakdown.transition_cost, color: "var(--series-optimized)", name: "Optimizer" },
      ],
    },
  ];

  return (
    <div className="page-in">
      {/* ---------- Hero ---------- */}
      <section className="hero">
        <div className="hero-eyebrow">
          <ExperimentBadge />
          <span style={{ fontSize: 12, color: "var(--text-3)" }}>
            Synthetic legal-storage workload · {formatCount(summary.total_documents)} documents ·{" "}
            {formatGb(summary.total_storage_gb)}
          </span>
        </div>
        <h1 className="hero-title">Storage Intelligence</h1>
        <p className="hero-sub">
          Understand where your storage budget is going — and what a
          workload-aware allocation would cost instead.
        </p>

        <div className="hero-grid">
          <div className="hero-stat">
            <div className="stat-label">Current annual cost</div>
            <div
              className="stat-value hero"
              style={{ fontVariantNumeric: "normal" }}
            >
              {formatMoney(summary.baseline_cost)}
            </div>
            <div className="stat-delta">
              Age-based lifecycle baseline · {formatCount(summary.baseline_transition_count)} transitions
            </div>
          </div>
          <div className="hero-stat">
            <div className="stat-label">Optimizer annual cost</div>
            <div className="stat-value hero">{formatMoney(summary.optimizer_cost)}</div>
            <div className="stat-delta">
              Cheapest eligibility-constrained tier per document
            </div>
          </div>
          <div
            className="hero-stat"
            style={{ borderColor: "var(--accent-border)" }}
          >
            <div className="stat-label">Potential savings</div>
            <div
              className="stat-value hero"
              style={{ color: "var(--accent-strong)" }}
            >
              {formatMoney(summary.optimizer_savings_vs_baseline)}
            </div>
            <div className="stat-delta good">
              {formatPercent(summary.optimizer_savings_percentage_vs_baseline, 1)} vs current
              baseline
            </div>
          </div>
        </div>
      </section>

      {/* ---------- Live system row: aggregation run + cost estimate ---------- */}
      <div className="grid-2 mt-3">
        <AggregationPanel benchmark={bundle.aggregation_benchmark} />
        <LiveCostSnapshot />
      </div>

      {/* ---------- Cost overview + tier distribution ---------- */}
      <div className="grid-2 mt-3">
        <section className="panel chart-card">
          <div className="panel-head">
            <div>
              <div className="panel-title">Why does storage cost money?</div>
              <div className="panel-note">
                Annual cost components, baseline vs optimizer allocation
              </div>
            </div>
            <ExperimentBadge />
          </div>
          <BarList
            entries={costEntries}
            formatValue={formatMoney}
            ariaLabel="Annual cost by component, baseline versus optimizer"
            tableCaption="Modeled 12-month cost components under the baseline age-based lifecycle and the optimizer recommendation (synthetic workload)."
          />
          <p
            className="panel-note"
            style={{ padding: "0 22px 16px", lineHeight: 1.6 }}
          >
            Retrieval fees dominate: the baseline parks rarely-accessed
            documents in archive tiers whose forecast retrieval load is
            expensive, while storage itself is cheap. The optimizer keeps
            frequently-accessed documents in hot tiers and the rest in retrieval
            classes it can actually afford.
          </p>
        </section>

        <section className="panel chart-card">
          <div className="panel-head">
            <div>
              <div className="panel-title">Storage tier allocation</div>
              <div className="panel-note">
                Document count and storage per tier
              </div>
            </div>
            <ExperimentBadge />
          </div>
          <StackedTierBar
            current={tierSegments(summary.baseline_tier_distribution)}
            recommended={tierSegments(summary.optimizer_tier_distribution)}
            ariaLabel="Tier allocation before and after optimization"
            tableCaption="Documents and storage per storage tier, baseline versus optimizer (synthetic workload)."
          />
          <p className="panel-note" style={{ padding: "0 22px 8px" }}>
            Hover a segment for documents, storage, and projection details.
          </p>
        </section>
      </div>

      {/* ---------- Component changes + corpus comparison ---------- */}
      <div className="grid-2 mt-3">
        <section className="panel chart-card">
          <div className="panel-head">
            <div>
              <div className="panel-title">Where the savings come from</div>
              <div className="panel-note">
                Modeled component changes — savings below the zero line, added
                cost above
              </div>
            </div>
            <ExperimentBadge />
          </div>
          <SavingsChangePanel
            summary={summary}
            ariaLabel="Modeled annual cost change per component (optimizer minus baseline); savings extend below the zero line"
          />
          <p className="panel-note" style={{ padding: "0 22px 16px", lineHeight: 1.6 }}>
            Every bar starts on the same $0 line. Bars below the line are
            modeled savings; a bar above it means the optimizer deliberately
            pays more there (storage fills safer, affordable tiers so
            retrieval fees collapse). The bars' changes sum to the optimizer's
            annual cost — hover or focus a bar for its baseline → optimizer
            values and the running annual total.
          </p>
        </section>
        <CorpusComparisonPanel bundle={bundle} />
      </div>

      {/* ---------- Savings opportunities ---------- */}
      <section className="panel mt-3">
        <div className="panel-head">
          <div>
            <div className="panel-title">Savings opportunities</div>
            <div className="panel-note">
              Modeled annual savings by destination tier · real judicial corpus
            </div>
          </div>
          <div className="row" style={{ gap: 8 }}>
            <ExperimentBadge />
            <Link to="/experiments" className="badge badge-neutral" style={{ textDecoration: "none" }}>
              All experiments <ArrowRight size={12} />
            </Link>
          </div>
        </div>
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fill, minmax(230px, 1fr))",
            gap: 14,
            padding: "16px 22px 20px",
          }}
        >
          {bundle.real.tier_opportunities.length > 0 &&
            bundle.real.tier_opportunities.map((opp) => (
              <div
                key={opp.tier}
                className="panel panel-pad"
                style={{ background: "var(--panel-2)" }}
              >
                <div className="row">
                  <span
                    className="tier-dot"
                    style={{
                      background: `var(--tier-${tierIndex(opp.tier) + 1})`,
                    }}
                  />
                  <strong style={{ fontSize: 13 }}>{opp.tier_label}</strong>
                </div>
                <div className="muted tnum" style={{ fontSize: 12, marginTop: 4 }}>
                  {formatCount(opp.document_count)} documents ·{" "}
                  {bundle.real.corpus.document_count} in corpus
                </div>
                <div
                  className="tnum"
                  style={{ fontSize: 22, fontWeight: 650, marginTop: 10, color: "var(--accent-strong)" }}
                >
                  {formatMoney(opp.annual_savings)}
                </div>
                <div className="stat-label" style={{ fontSize: 11 }}>
                  potential reduction / year
                </div>
                <Link
                  to={`/documents?tier=${encodeURIComponent(opp.tier)}`}
                  className="row"
                  style={{ marginTop: 12, fontSize: 12.5, color: "var(--info)" }}
                >
                  View documents <ArrowRight size={13} />
                </Link>
              </div>
            ))}

          {bundle.real.tier_opportunities.length === 0 && (
            <div style={{ gridColumn: "1/-1" }}>
              <EmptyState
                title="No positive-savings tier in this corpus"
                description="The real court corpus is small and rarely accessed, yet every modeled migration still lands in a cheaper eligible tier — the roll-up shows no positive-savings opportunity."
                actions={
                  <Link className="btn" to="/experiments">
                    <FlaskConical size={14} /> View experiment results
                  </Link>
                }
              />
            </div>
          )}
        </div>
        <p className="panel-note" style={{ padding: "0 22px 18px", lineHeight: 1.6 }}>
          Dollar figures are modeled projections from the optimizer's pricing
          snapshot for {PRICING_REGION} over a 12-month horizon — not AWS invoices.
          The court corpus is 500 genuinely public U.S. District Court opinions
          (District of Columbia, 2008).
        </p>
      </section>
    </div>
  );
}