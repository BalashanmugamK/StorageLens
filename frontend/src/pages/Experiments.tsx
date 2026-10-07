// Experiments — the research side of StorageLens. Four clearly
// separated controlled runs over legal-document corpora. Every number
// here is EXPERIMENTAL DATA from offline artifacts and is labeled as
// such; where a row's document id exists in the live system it links
// through to the live detail page without mixing provenance.

import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { ArrowRight, FlaskConical, Globe, Layers, Search, Shuffle } from "lucide-react";
import type {
  ExperimentBundle,
  OptimizeRunDoc,
} from "../types";
import { TIER_ORDER } from "../types";
import { loadExperiments } from "../data/experiments";
import { useLiveSystem } from "../hooks/useLiveSystem";
import { ColumnHistogram } from "../components/charts/ColumnHistogram";
import { StackedTierBar } from "../components/charts/StackedTierBar";
import type { TierSegment } from "../components/charts/StackedTierBar";
import { BarList } from "../components/charts/BarList";
import { ExperimentBadge, Pagination, TierDot, tierIndex } from "../components/ui/atoms";
import {
  formatDate,
  formatBytes,
  formatCount,
  formatGb,
  formatMoney,
  formatPercent,
  tierLabel,
} from "../utils/format";
import { PRICING_CURRENCY, PRICING_REGION } from "../utils/pricing";

const TIER_COLORS = [
  "var(--tier-1)",
  "var(--tier-2)",
  "var(--tier-3)",
  "var(--tier-4)",
  "var(--tier-5)",
  "var(--tier-6)",
];

const PAGE_SIZE = 20;

type ExperimentKey = "real" | "diversified" | "synthetic" | "baseline";

/* ---------- Shared chart helpers ---------- */

function tierSegments(counts: Map<string, { count: number; gb: number }>): TierSegment[] {
  return TIER_ORDER.filter((tier) => counts.get(tier)?.count).map((tier) => {
    const entry = counts.get(tier)!;
    return {
      tier,
      tierLabel: tierLabel(tier),
      color: TIER_COLORS[tierIndex(tier)],
      documentCount: entry.count,
      storageGb: entry.gb,
    };
  });
}

function runTierRollup(documents: OptimizeRunDoc[]) {
  const currentCounts = new Map<string, { count: number; gb: number }>();
  const recommendedCounts = new Map<string, { count: number; gb: number }>();
  for (const doc of documents) {
    for (const [map, tier] of [
      [currentCounts, doc.current_storage_class],
      [recommendedCounts, doc.recommended_storage_class],
    ] as const) {
      const entry = map.get(tier) ?? { count: 0, gb: 0 };
      entry.count += 1;
      entry.gb += doc.file_size_bytes / 1024 ** 3;
      map.set(tier, entry);
    }
  }
  return { current: tierSegments(currentCounts), recommended: tierSegments(recommendedCounts) };
}

function syntheticSegments(
  distribution: Record<string, { document_count: number; storage_gb: number }>,
): TierSegment[] {
  return TIER_ORDER.filter((tier) => distribution[tier]?.document_count).map((tier) => ({
    tier,
    tierLabel: tierLabel(tier),
    color: TIER_COLORS[tierIndex(tier)],
    documentCount: distribution[tier].document_count,
    storageGb: distribution[tier].storage_gb,
  }));
}

/* ---------- Results table ---------- */

interface ResultRow {
  key: string;
  name: string;
  linkTo: string | null;
  meta: React.ReactNode;
  size: number;
  current: string;
  recommended: string;
  savings: number;
  savingsPercentage: number | null;
  verdict: string | null;
}

function ResultsTable({
  rows,
  page,
  pageCount,
  onPage,
  query,
  onQuery,
  searchLabel,
  liveCount,
}: {
  rows: ResultRow[];
  page: number;
  pageCount: number;
  onPage: (page: number) => void;
  query: string;
  onQuery: (q: string) => void;
  searchLabel: string;
  liveCount: number;
}) {
  const savingsColor = (value: number) =>
    value > 0
      ? "var(--accent-strong)"
      : value < 0
        ? "var(--danger)"
        : "var(--text-3)";

  return (
    <section className="panel mt-3">
      <div className="panel-head">
        <div className="panel-title">Document-level results</div>
        <span className="row muted" style={{ gap: 5, fontSize: 12 }}>
          <Search size={13} />
          <input
            className="input"
            placeholder={searchLabel}
            value={query}
            onChange={(e) => onQuery(e.target.value)}
            style={{ width: 220 }}
            aria-label={searchLabel}
          />
        </span>
      </div>
      <div className="table-wrap">
        <table className="data-table">
          <thead>
            <tr>
              <th>Document</th>
              <th className="num">Size</th>
              <th>Tier move</th>
              <th className="num">Modeled savings / yr</th>
              <th>Verdict</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const move = row.recommended !== row.current;
              const nameInner = (
                <span className="doc-cell" style={{ minWidth: 160 }}>
                  <span className="doc-ico" style={{ fontSize: 7.5, fontWeight: 750 }}>
                    PDF
                  </span>
                  <span style={{ minWidth: 0 }}>
                    <span className="doc-name">{row.name}</span>
                    {row.meta}
                  </span>
                </span>
              );
              return (
                <tr key={row.key}>
                  <td>
                    {row.linkTo ? (
                      <Link
                        to={row.linkTo}
                        style={{ textDecoration: "none", color: "inherit" }}
                        title="Open in live documents"
                      >
                        {nameInner}
                      </Link>
                    ) : (
                      nameInner
                    )}
                  </td>
                  <td className="num tnum">{formatBytes(row.size)}</td>
                  <td>
                    <span className="row" style={{ gap: 6, flexWrap: "nowrap" }}>
                      <span className="tier-pill row" style={{ gap: 6 }}>
                        <TierDot tier={row.current} />
                        {tierLabel(row.current)}
                      </span>
                      {move && (
                        <>
                          <span className="muted">→</span>
                          <span
                            className="tier-pill row"
                            style={{
                              gap: 6,
                              color: row.savings > 0 ? "var(--accent-strong)" : undefined,
                            }}
                          >
                            <TierDot tier={row.recommended} />
                            {tierLabel(row.recommended)}
                          </span>
                        </>
                      )}
                    </span>
                  </td>
                  <td className="num">
                    <span className="tnum" style={{ color: savingsColor(row.savings) }}>
                      {row.savings !== 0
                        ? `${row.savings > 0 ? "" : "−"}${formatMoney(
                            Math.abs(row.savings),
                          )}${
                            row.savingsPercentage !== null
                              ? ` (${formatPercent(row.savingsPercentage)})`
                              : ""
                          }`
                        : "—"}
                    </span>
                  </td>
                  <td>
                    <span
                      className={
                        row.savings > 0
                          ? "badge badge-experiment"
                          : row.savings < 0
                            ? "badge badge-danger"
                            : "badge badge-neutral"
                      }
                    >
                      {row.verdict ??
                        (row.savings > 0 ? "Positive" : row.savings < 0 ? "Negative" : "No change")}
                    </span>
                  </td>
                </tr>
              );
            })}
            {rows.length === 0 && (
              <tr>
                <td colSpan={5} style={{ textAlign: "center", padding: "26px 0", color: "var(--text-3)" }}>
                  No documents match that search.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      {liveCount > 0 && (
        <div
          className="row"
          style={{
            padding: "10px 16px 14px",
            borderTop: "1px solid var(--border)",
            fontSize: 11.5,
            color: "var(--text-3)",
            gap: 8,
          }}
        >
          <span className="badge badge-live">Live</span>
          <span>
            {liveCount.toLocaleString()} rows link to live document records in
            this deployment — their experiment values stay badged there.
          </span>
        </div>
      )}
      <Pagination page={page} pageCount={pageCount} onPage={onPage} totalItems={rows.length} />
    </section>
  );
}

/* ---------- Optimize-run results (real / diversified) ---------- */

function cleanVerdict(verdict: string): string | null {
  const text = verdict.replace(/_/g, " ").toLowerCase();
  if (text.startsWith("positive")) return "Positive";
  if (text.startsWith("negative")) return "Negative";
  if (text.startsWith("no change")) return "No change";
  return verdict || null;
}

function runRowMeta(doc: OptimizeRunDoc, experimentKey: "real" | "diversified") {
  if (experimentKey === "diversified") {
    const bits = [doc.collection_label ?? doc.collection, doc.document_state]
      .filter(Boolean)
      .join(" · ");
    return bits ? <span className="doc-sub">{bits}</span> : null;
  }
  const date = doc.date_issued ? formatDate(doc.date_issued) : null;
  const bits = [doc.court ? "U.S. District D.C." : null, date].filter(Boolean).join(" · ");
  return bits ? <span className="doc-sub">{bits}</span> : null;
}

function ExperimentRunHeader({
  experimentKey,
  bundle,
  corpusBytes,
}: {
  experimentKey: "real" | "diversified";
  bundle: ExperimentBundle;
  corpusBytes: number;
}) {
  const header =
    experimentKey === "diversified" ? bundle.diversified.header : bundle.real.header;
  const blurb =
    experimentKey === "diversified"
      ? "500 real GovInfo documents (court opinions, U.S. Code, CFR) spanning mixed storage tiers and document states — access behavior, storage class and retention state are the controlled experimental variables."
      : "500 public U.S. District Court opinions from 2008 (District of Columbia) loaded into the live backend as real S3 objects and DynamoDB records, then optimized as one batch.";

  const stats: { label: string; value: string }[] = [
    { label: "Documents", value: formatCount(header.documents_processed) },
    { label: "Corpus size", value: formatBytes(corpusBytes) },
    { label: "Access events", value: formatCount(header.access_events_on_record) },
    {
      label: "Optimizer success",
      value: `${formatCount(header.optimizer_success)} / ${formatCount(header.documents_processed)}`,
    },
    { label: "Modeled savings", value: formatPercent(header.savings_percentage) },
    { label: "Optimized annual", value: formatMoney(header.recommended_projected_annual_cost) },
  ];

  return (
    <>
      <div className="row" style={{ justifyContent: "space-between", flexWrap: "wrap", gap: 10 }}>
        <div>
          <h2 style={{ fontSize: 16 }}>
            {experimentKey === "diversified"
              ? "Diversified workload — controlled scenarios"
              : "Real legal corpus — U.S. District Court, D.C. (2008)"}
          </h2>
          <p
            style={{
              color: "var(--text-2)",
              fontSize: 12.5,
              marginTop: 4,
              maxWidth: 640,
              lineHeight: 1.6,
            }}
          >
            {blurb}
          </p>
        </div>
        <ExperimentBadge />
      </div>
      <div
        className="mt-2"
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(126px, 1fr))",
          gap: 10,
        }}
      >
        {stats.map((stat) => (
          <div
            key={stat.label}
            style={{
              border: "1px solid var(--border)",
              borderRadius: "var(--radius-sm)",
              padding: "10px 12px",
              background: "var(--panel-2)",
            }}
          >
            <div className="stat-label" style={{ fontSize: 11 }}>
              {stat.label}
            </div>
            <div className="tnum" style={{ fontSize: 15.5, fontWeight: 650, marginTop: 2 }}>
              {stat.value}
            </div>
          </div>
        ))}
      </div>
    </>
  );
}

function RunResults({
  experimentKey,
  bundle,
}: {
  experimentKey: "real" | "diversified";
  bundle: ExperimentBundle;
}) {
  const { documents: liveDocuments } = useLiveSystem();
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(1);

  const liveIds = useMemo(
    () => new Set(liveDocuments.map((d) => d.document_id)),
    [liveDocuments],
  );

  const experiment = experimentKey === "diversified" ? bundle.diversified : bundle.real;
  const documents = useMemo(() => Object.values(experiment.documents), [experiment]);
  const rollup = runTierRollup(documents);
  const header = experiment.header;

  useEffect(() => {
    setPage(1);
  }, [query, experimentKey]);

  const rows = useMemo(() => {
    const q = query.trim().toLowerCase();
    const filtered = q
      ? documents.filter(
          (doc) =>
            doc.manifest_document_id.toLowerCase().includes(q) ||
            (doc.title ?? "").toLowerCase().includes(q),
        )
      : documents;
    return filtered.sort((a, b) => b.savings - a.savings);
  }, [documents, query]);

  const visible = rows.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);
  const liveCount = documents.filter((doc) => liveIds.has(doc.document_id)).length;

  const positiveDocs = documents.filter((d) => d.savings > 0);
  const savingsBands = [
    { label: "0–10%", min: 0.001, max: 10 },
    { label: "10–30%", min: 10, max: 30 },
    { label: "30–60%", min: 30, max: 60 },
    { label: "60–85%", min: 60, max: 85 },
    { label: "85–100%", min: 85, max: 100.001 },
  ].map((band) => ({
    label: band.label,
    value: positiveDocs.filter(
      (d) => d.savings_percentage >= band.min && d.savings_percentage < band.max,
    ).length,
    color: "var(--div-pos)",
  }));

  const corpusBytes =
    experimentKey === "real"
      ? bundle.real.corpus.total_bytes
      : documents.reduce((sum, doc) => sum + doc.file_size_bytes, 0);

  return (
    <div className="mt-3">
      <section className="panel panel-pad">
        <ExperimentRunHeader
          experimentKey={experimentKey}
          bundle={bundle}
          corpusBytes={corpusBytes}
        />
      </section>

      <div className="grid-2 mt-3">
        <section className="panel chart-card">
          <div className="panel-head">
            <div className="panel-title">Verdict distribution</div>
            <ExperimentBadge />
          </div>
          <ColumnHistogram
            bins={[
              { label: "Positive", value: header.positive_savings_count, color: "var(--div-pos)" },
              { label: "No change", value: header.no_change_count, color: "var(--div-mid)" },
              { label: "Negative", value: header.negative_savings_count, color: "var(--div-neg)" },
            ]}
            formatValue={formatCount}
            ariaLabel="Documents by verdict"
            tableCaption="Verdict counts across this experiment run."
          />
          <p className="panel-note" style={{ padding: "0 22px 16px", lineHeight: 1.6 }}>
            Under the engine's Policy A a negative verdict means a
            state-eligible migration models as more expensive than staying put —
            those become Policy B conflicts on the Policies page.
          </p>
        </section>

        <section className="panel chart-card">
          <div className="panel-head">
            <div className="panel-title">Positive-savings depth</div>
            <ExperimentBadge />
          </div>
          <ColumnHistogram
            bins={savingsBands}
            formatValue={formatCount}
            ariaLabel="Positive-savings documents by savings depth"
            tableCaption="Positive-savings documents bucketed by modeled savings percentage."
          />
        </section>
      </div>

      <section className="panel mt-3 chart-card">
        <div className="panel-head">
          <div className="panel-title">Tier allocation</div>
          <ExperimentBadge />
        </div>
        <StackedTierBar
          current={rollup.current}
          recommended={rollup.recommended}
          ariaLabel="Tier allocation, current versus recommended"
          tableCaption="Documents and storage per tier, current versus recommended."
        />
      </section>

      <ResultsTable
        rows={visible.map((doc) => ({
          key: doc.manifest_document_id,
          name:
            experimentKey === "diversified" && doc.title
              ? doc.title
              : doc.manifest_document_id,
          linkTo: liveIds.has(doc.document_id) ? `/documents/${doc.document_id}` : null,
          meta: runRowMeta(doc, experimentKey),
          size: doc.file_size_bytes,
          current: doc.current_storage_class,
          recommended: doc.recommended_storage_class,
          savings: doc.savings,
          savingsPercentage: doc.savings_percentage,
          verdict: cleanVerdict(doc.verdict),
        }))}
        page={page}
        pageCount={Math.max(Math.ceil(rows.length / PAGE_SIZE), 1)}
        onPage={setPage}
        query={query}
        onQuery={setQuery}
        searchLabel={
          experimentKey === "diversified" ? "Search titles or ids…" : "Search manifest ids…"
        }
        liveCount={liveCount}
      />
    </div>
  );
}

/* ---------- Synthetic / baseline results ---------- */

const BREAKDOWN_KEYS = ["retrieval_cost", "storage_cost", "request_cost", "transition_cost"] as const;

function SyntheticResults({
  isBaseline,
  bundle,
}: {
  isBaseline: boolean;
  bundle: ExperimentBundle;
}) {
  const summary = bundle.synthetic.summary;

  const breakdownEntries = ["Retrieval", "Storage", "Requests", "Transitions"].map(
    (label, i) => ({
      label,
      series: [
        {
          value: summary.baseline_cost_breakdown[BREAKDOWN_KEYS[i]],
          color: "var(--series-current)",
          name: "Baseline",
        },
        {
          value: summary.optimizer_cost_breakdown[BREAKDOWN_KEYS[i]],
          color: "var(--series-optimized)",
          name: "Optimizer",
        },
      ],
    }),
  );

  return (
    <div className="mt-3">
      <section className="panel panel-pad">
        <div className="row" style={{ justifyContent: "space-between", flexWrap: "wrap", gap: 10 }}>
          <div>
            <h2 style={{ fontSize: 16 }}>
              {isBaseline
                ? "Baseline — age-based lifecycle vs the optimizer"
                : "Synthetic workload — optimizer evaluation"}
            </h2>
            <p
              style={{
                color: "var(--text-2)",
                fontSize: 12.5,
                marginTop: 4,
                maxWidth: 640,
                lineHeight: 1.6,
              }}
            >
              {isBaseline
                ? "The conventional alternative: move documents on storage age alone (Standard → Standard-IA at 30 d → Flexible at 90 d → Deep Archive at 180 d). It ignores access behavior — and pays the forecast retrieval bill that follows."
                : "A deterministic seeded workload mimicking a law firm: 500 heavy documents, mixed tiers and wide-ranging 30-day access patterns — the optimizer's evaluation key."}
            </p>
          </div>
          <ExperimentBadge />
        </div>
        <div
          className="mt-2"
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(126px, 1fr))",
            gap: 10,
          }}
        >
          {[
            { label: "Documents", value: formatCount(summary.total_documents) },
            { label: "Corpus size", value: formatGb(summary.total_storage_gb) },
            { label: "Baseline annual", value: formatMoney(summary.baseline_cost) },
            { label: "Optimizer annual", value: formatMoney(summary.optimizer_cost) },
            {
              label: "Savings",
              value: formatPercent(summary.optimizer_savings_percentage_vs_baseline),
            },
          ].map((stat) => (
            <div
              key={stat.label}
              style={{
                border: "1px solid var(--border)",
                borderRadius: "var(--radius-sm)",
                padding: "10px 12px",
                background: "var(--panel-2)",
              }}
            >
              <div className="stat-label" style={{ fontSize: 11 }}>
                {stat.label}
              </div>
              <div className="tnum" style={{ fontSize: 15.5, fontWeight: 650, marginTop: 2 }}>
                {stat.value}
              </div>
            </div>
          ))}
        </div>
      </section>

      <div className="grid-2 mt-3">
        <section className="panel chart-card">
          <div className="panel-head">
            <div className="panel-title">Cost components</div>
            <ExperimentBadge />
          </div>
          <BarList
            entries={breakdownEntries}
            formatValue={formatMoney}
            ariaLabel="Annual cost components, baseline versus optimizer"
            tableCaption="Modeled cost components for the synthetic workload."
          />
          <p className="panel-note" style={{ padding: "0 22px 16px", lineHeight: 1.6 }}>
            {isBaseline
              ? `The baseline pays ${formatMoney(summary.baseline_cost_breakdown.retrieval_cost)} in forecast retrieval fees — its age ladder parks heavy, still-accessed objects in archive tiers.`
              : `The optimizer keeps ${formatCount(summary.optimizer_tier_distribution.STANDARD?.document_count ?? 0)} documents in Standard — its storage share rises so its retrieval share can fall.`}
          </p>
        </section>

        <section className="panel chart-card">
          <div className="panel-head">
            <div className="panel-title">Tier allocation</div>
            <ExperimentBadge />
          </div>
          <StackedTierBar
            current={syntheticSegments(summary.baseline_tier_distribution)}
            recommended={syntheticSegments(summary.optimizer_tier_distribution)}
            ariaLabel="Tier allocation, baseline versus optimizer"
            tableCaption="Documents and storage per tier for the synthetic workload."
          />
        </section>
      </div>

      <SyntheticTable bundle={bundle} />
    </div>
  );
}

/* ---------- Synthetic doc table ---------- */

function SyntheticTable({ bundle }: { bundle: ExperimentBundle }) {
  const { documents: liveDocuments } = useLiveSystem();
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(1);

  const liveIds = useMemo(
    () => new Set(liveDocuments.map((d) => d.document_id)),
    [liveDocuments],
  );

  const all = useMemo(() => Object.values(bundle.synthetic.documents), [bundle]);

  useEffect(() => setPage(1), [query]);

  const rows = useMemo(() => {
    const q = query.trim().toLowerCase();
    const filtered = q
      ? all.filter((doc) => doc.document_id.toLowerCase().includes(q))
      : all;
    return filtered.sort((a, b) => b.optimizer_savings - a.optimizer_savings);
  }, [all, query]);

  const visible = rows.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);
  const liveCount = all.filter((doc) => liveIds.has(doc.document_id)).length;

  return (
    <ResultsTable
      rows={visible.map((doc) => ({
        key: doc.document_id,
        name: doc.document_id,
        linkTo: liveIds.has(doc.document_id) ? `/documents/${doc.document_id}` : null,
        meta: null,
        size: doc.file_size_bytes,
        current: doc.current_storage_class,
        recommended: doc.optimizer_storage_class,
        savings: doc.optimizer_savings,
        savingsPercentage: doc.optimizer_savings_percentage,
        verdict: null,
      }))}
      page={page}
      pageCount={Math.max(Math.ceil(rows.length / PAGE_SIZE), 1)}
      onPage={setPage}
      query={query}
      onQuery={setQuery}
      searchLabel="Search document ids…"
      liveCount={liveCount}
    />
  );
}

/* ---------- Page ---------- */

interface ExperimentCardMeta {
  key: ExperimentKey;
  title: string;
  subtitle: string;
  icon: React.ReactNode;
  stats: { label: string; value: string }[];
  blurb: string;
}

function moneyShort(value: number): string {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: PRICING_CURRENCY,
    maximumFractionDigits: 0,
  }).format(value);
}

export default function Experiments() {
  const [bundle, setBundle] = useState<ExperimentBundle | null>(null);
  const [selected, setSelected] = useState<ExperimentKey | null>(null);

  useEffect(() => {
    loadExperiments().then(setBundle);
  }, []);

  const cards: ExperimentCardMeta[] = bundle
    ? [
        {
          key: "real",
          title: "Real legal corpus",
          subtitle: "GovInfo · U.S. District Court opinions",
          icon: <Globe size={16} strokeWidth={1.7} />,
          stats: [
            {
              label: "Documents",
              value: formatCount(bundle.real.corpus.document_count),
            },
            { label: "Corpus", value: formatBytes(bundle.real.corpus.total_bytes) },
            { label: "Year", value: bundle.real.corpus.year.join(", ") },
            { label: "Savings", value: formatPercent(bundle.real.header.savings_percentage) },
          ],
          blurb: `${formatCount(bundle.real.corpus.document_count)} public U.S. District Court opinions (D.D.C., ${bundle.real.corpus.year.join(", ")}) fetched from GovInfo and loaded into the live backend as real S3 objects and DynamoDB records.`,
        },
        {
          key: "diversified",
          title: "Diversified workload",
          subtitle: "Controlled access + state variables",
          icon: <Shuffle size={16} strokeWidth={1.7} />,
          stats: [
            {
              label: "Documents",
              value: formatCount(
                Object.keys(bundle.diversified.documents).length,
              ),
            },
            {
              label: "State bands",
              value: formatCount(
                Object.keys(bundle.diversified.header.document_state_counts)
                  .length,
              ),
            },
            {
              label: "Access events",
              value: formatCount(bundle.diversified.header.access_events_on_record),
            },
            { label: "Savings", value: formatPercent(bundle.diversified.header.savings_percentage) },
          ],
          blurb:
            "Real GovInfo documents spanning mixed storage tiers and document states — access behavior, storage class and retention are controlled experimental variables.",
        },
        {
          key: "synthetic",
          title: "Synthetic workload",
          subtitle: "Deterministic law-firm simulation",
          icon: <Layers size={16} strokeWidth={1.7} />,
          stats: [
            {
              label: "Documents",
              value: formatCount(bundle.synthetic.summary.total_documents),
            },
            { label: "Corpus", value: formatGb(bundle.synthetic.summary.total_storage_gb) },
            { label: "Baseline annual", value: moneyShort(bundle.synthetic.summary.baseline_cost) },
            {
              label: "Savings",
              value: formatPercent(
                bundle.synthetic.summary.optimizer_savings_percentage_vs_baseline,
              ),
            },
          ],
          blurb:
            "A deterministic seeded workload mimicking a law firm: heavy documents, mixed storage classes, wide-ranging access patterns — the optimizer's evaluation key.",
        },
        {
          key: "baseline",
          title: "Baseline",
          subtitle: "Age-based lifecycle comparison",
          icon: <FlaskConical size={16} strokeWidth={1.7} />,
          stats: [
            { label: "Strategy", value: "30 / 90 / 180 d ladder" },
            {
              label: "Transitions",
              value: formatCount(bundle.synthetic.summary.baseline_transition_count),
            },
            { label: "Baseline annual", value: moneyShort(bundle.synthetic.summary.baseline_cost) },
            { label: "Optimizer annual", value: moneyShort(bundle.synthetic.summary.optimizer_cost) },
          ],
          blurb:
            "The conventional alternative: move documents on storage age alone. It ignores access behavior — and pays for it in retrieval fees.",
        },
      ]
    : [];

  const viewResults = (key: ExperimentKey) => {
    setSelected(key);
    requestAnimationFrame(() => {
      document.getElementById("results")?.scrollIntoView({ behavior: "smooth" });
    });
  };

  return (
    <div className="page-in">
      <header
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
          <h1 style={{ fontSize: 21, letterSpacing: "-0.02em" }}>Experiments</h1>
          <p style={{ color: "var(--text-2)", marginTop: 4, fontSize: 13.5 }}>
            Controlled, reproducible evaluation runs over legal-document corpora.
          </p>
        </div>
        <ExperimentBadge note="Everything on this page is experimental data" />
      </header>

      <div
        className="panel panel-pad"
        style={{ marginBottom: 20, borderLeft: "3px solid var(--info-border)" }}
      >
        <p style={{ color: "var(--text-2)", fontSize: 12.5, lineHeight: 1.65 }}>
          <strong style={{ color: "var(--text)" }}>Methodology. </strong>
          The corpora contain genuinely public legal documents (GovInfo; U.S.
          federal works). Access behavior, storage class and retention state are
          controlled experimental variables: they exercise the optimizer against
          realistic legal workloads without claiming to be production telemetry.
          Every figure on this page is a modeled 12-month projection from the
          pricing snapshot for {PRICING_REGION}, produced by offline optimizer runs —
          none of it is live system output.
        </p>
      </div>

      {/* Every dataset the experiments run over — one place, with the
          synthetic/real split the rest of the app uses (Documents page
          Source filter). */}
      {bundle && (
        <section className="panel panel-pad" style={{ marginBottom: 20 }}>
          <div className="panel-head">
            <div className="panel-title">Datasets used</div>
          </div>
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Experiment</th>
                  <th>Dataset</th>
                  <th>Kind</th>
                  <th className="num">Docs</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td>Synthetic workload</td>
                  <td style={{ overflowWrap: "anywhere" }}>
                    data/synthetic/workload_500.json — engineered law-firm
                    simulation (sizes, tiers, access and states chosen by the
                    experiment design)
                  </td>
                  <td>
                    <span className="badge badge-experiment">Synthetic</span>
                  </td>
                  <td className="num tnum">
                    {formatCount(bundle.synthetic.summary.total_documents)}
                  </td>
                </tr>
                <tr>
                  <td>Diversified workload</td>
                  <td style={{ overflowWrap: "anywhere" }}>
                    data/real/diversified/manifest.json — real GovInfo
                    documents (court opinions, U.S. Code, CFR); access behavior,
                    storage class and state are the controlled variables
                  </td>
                  <td>
                    <span className="badge badge-neutral">Real · GovInfo</span>
                  </td>
                  <td className="num tnum">
                    {formatCount(bundle.diversified.header.documents_processed)}
                  </td>
                </tr>
                <tr>
                  <td>Real legal corpus</td>
                  <td style={{ overflowWrap: "anywhere" }}>
                    data/real/us_courts/manifest.json — public U.S. District
                    Court opinions (D.D.C., 2008), {formatBytes(bundle.real.corpus.total_bytes)}
                  </td>
                  <td>
                    <span className="badge badge-neutral">Real · GovInfo</span>
                  </td>
                  <td className="num tnum">
                    {formatCount(bundle.real.header.documents_processed)}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <p className="panel-note" style={{ padding: "8px 22px 4px", lineHeight: 1.6 }}>
            The same synthetic seeds and real GovInfo files are loaded into the
            live S3/DynamoDB backend, so the{" "}
            <Link to="/documents">Documents</Link> inventory mixes both —
            filter it with its Source selector (Synthetic / Real).
          </p>
        </section>
      )}

      {!bundle && (
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fill, minmax(250px, 1fr))",
            gap: 14,
          }}
        >
          {[0, 1, 2, 3].map((i) => (
            <div key={i} className="skeleton" style={{ height: 250, borderRadius: "var(--radius)" }} />
          ))}
        </div>
      )}

      {bundle && (
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fill, minmax(250px, 1fr))",
            gap: 14,
            marginBottom: 8,
          }}
        >
          {cards.map((card) => (
            <section
              key={card.key}
              className="panel panel-pad"
              style={{
                display: "flex",
                flexDirection: "column",
                gap: 10,
                borderColor: selected === card.key ? "var(--accent-border)" : undefined,
              }}
            >
              <span className="row muted" style={{ gap: 8 }}>
                {card.icon}
                <span style={{ fontSize: 12 }}>{card.subtitle}</span>
              </span>
              <h3 style={{ fontSize: 14.5 }}>{card.title}</h3>
              <p style={{ color: "var(--text-2)", fontSize: 12.5, lineHeight: 1.6, flex: 1 }}>
                {card.blurb}
              </p>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
                {card.stats.map((stat) => (
                  <div
                    key={stat.label}
                    style={{
                      background: "var(--panel-2)",
                      borderRadius: "var(--radius-sm)",
                      padding: "8px 10px",
                    }}
                  >
                    <div className="stat-label" style={{ fontSize: 10.5 }}>
                      {stat.label}
                    </div>
                    <div className="tnum" style={{ fontWeight: 600, fontSize: 13, marginTop: 1 }}>
                      {stat.value}
                    </div>
                  </div>
                ))}
              </div>
              <button type="button" className="btn" onClick={() => viewResults(card.key)}>
                View results <ArrowRight size={13} />
              </button>
            </section>
          ))}
        </div>
      )}

      {selected && bundle && (
        <div id="results">
          {selected === "real" && <RunResults experimentKey="real" bundle={bundle} />}
          {selected === "diversified" && (
            <RunResults experimentKey="diversified" bundle={bundle} />
          )}
          {selected === "synthetic" && <SyntheticResults isBaseline={false} bundle={bundle} />}
          {selected === "baseline" && <SyntheticResults isBaseline bundle={bundle} />}
        </div>
      )}
    </div>
  );
}