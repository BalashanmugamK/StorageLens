// LiveCostSnapshot — a current-state cost read over the LIVE documents.
//
// Honesty contract: the dollar figures are an ESTIMATE computed
// client-side from live document metadata (file_size,
// current_storage_class) using the optimizer's own pricing snapshot
// (region/currency from src/utils/pricing.ts, which the vitest suite
// pins to the snapshot's meta). They are not an AWS invoice and not
// live AWS pricing. Deliberately NO savings figure here: estimating "potential
// savings" over live data would mix LIVE and EXPERIMENT data in one
// number, which this app never does. Savings appear only in
// EXPERIMENT-labeled panels.

import { Link } from "react-router-dom";
import { AlertTriangle, Upload } from "lucide-react";
import { useLiveSystem } from "../../hooks/useLiveSystem";
import {
  EmptyState,
  LiveBadge,
  SignedOutNote,
  StatTile,
  tierIndex,
} from "../ui/atoms";
import { BarList } from "../charts/BarList";
import { formatBytes, formatCount, formatGb, formatMoney, tierLabel } from "../../utils/format";
import {
  estimateLiveStorageCost,
  ESTIMATE_TIER_ORDER,
  PRICING_CHECKED,
  PRICING_REGION,
  PRICING_SOURCE,
} from "../../utils/pricing";

// Color follows the tier's identity, not its bar position — an
// unknown class is never painted as a real tier color.
function tierColor(tier: string): string {
  return ESTIMATE_TIER_ORDER.includes(tier)
    ? `var(--tier-${tierIndex(tier) + 1})`
    : "var(--series-current)";
}

function OfflineNote({ error }: { error: string | null }) {
  return (
    <div
      className="row"
      role="status"
      style={{
        gap: 8,
        border: "1px solid var(--warning-border)",
        background: "var(--warning-dim)",
        borderRadius: "var(--radius-sm)",
        padding: "8px 12px",
        fontSize: 12.5,
        color: "var(--text-2)",
      }}
    >
      <AlertTriangle size={14} style={{ color: "var(--warning)", flex: "none" }} />
      <span>
        API unreachable ({error ?? "unknown error"}) — showing the snapshot
        from the last successful fetch.
      </span>
    </div>
  );
}

export function LiveCostSnapshot() {
  const { status, documents, error } = useLiveSystem();

  if (status === "unconfigured") {
    return (
      <section className="panel chart-card">
        <SnapshotHead />
        <EmptyState
          title="No API base URL configured"
          description="Point StorageLens at the backend API in Settings to see a live storage cost estimate."
          actions={
            <Link className="btn" to="/settings">
              Open Settings
            </Link>
          }
        />
      </section>
    );
  }

  if (status === "connecting") {
    return (
      <section className="panel chart-card" aria-busy="true">
        <SnapshotHead />
        <div style={{ padding: "18px 22px 22px" }} aria-hidden="true">
          <div className="row" style={{ gap: 12 }}>
            <div className="skeleton" style={{ height: 72, flex: 1 }} />
            <div className="skeleton" style={{ height: 72, flex: 1 }} />
            <div className="skeleton" style={{ height: 72, flex: 1 }} />
          </div>
          <div className="skeleton skeleton-row mt-3" />
          <div className="skeleton skeleton-row" />
          <div className="skeleton skeleton-row" />
        </div>
      </section>
    );
  }

  if (documents.length === 0) {
    return (
      <section className="panel chart-card">
        <SnapshotHead />
        <EmptyState
          title="No documents in the live system yet"
          description="Upload a document and this panel will estimate its billable storage and monthly cost from the live metadata."
          actions={
            <Link className="btn" to="/documents">
              <Upload size={14} /> Go to documents
            </Link>
          }
        />
        {status === "signed-out" && (
          <div style={{ padding: "0 22px" }}>
            <SignedOutNote />
          </div>
        )}
        {status === "offline" && <OfflineNote error={error} />}
      </section>
    );
  }

  const estimate = estimateLiveStorageCost(documents);

  return (
    <section className="panel chart-card">
      <SnapshotHead />
      {status === "offline" && (
        <div style={{ padding: "0 22px" }}>
          <OfflineNote error={error} />
        </div>
      )}
      <div
        className="row"
        style={{
          gap: 12,
          alignItems: "stretch",
          padding: "16px 22px 4px",
          flexWrap: "wrap",
        }}
      >
        <StatTile
          label="Billable storage"
          value={formatGb(estimate.totalBillableGb)}
          delta={`${formatCount(documents.length)} documents (live)`}
          footer={
            <div className="stat-label" style={{ fontSize: 11 }}>
              after minimum-billable sizing
            </div>
          }
        />
        <StatTile
          label="Estimated monthly"
          value={formatMoney(estimate.totalMonthly, { precision: 2 })}
          delta="storage charges only"
        />
        <StatTile
          label="Estimated annual"
          value={formatMoney(estimate.totalAnnual, { precision: 2 })}
          delta="= monthly × 12, current allocation"
        />
      </div>
      {estimate.tiers.length > 0 && (
        <div style={{ padding: "14px 22px 0" }}>
          <BarList
            entries={estimate.tiers.map((tier) => ({
              label: tierLabel(tier.tier),
              series: [
                {
                  value: tier.monthlyCost,
                  color: tierColor(tier.tier),
                  name: "Estimated monthly",
                },
              ],
              callout: `${formatCount(tier.documentCount)} docs · ${formatGb(tier.billableGb)}`,
            }))}
            formatValue={(v) => formatMoney(v)}
            ariaLabel="Estimated monthly storage cost per live storage tier"
            tableCaption="Estimated monthly storage cost per tier over the live documents (client-side estimate from the optimizer's pricing snapshot)."
          />
        </div>
      )}
      {estimate.unknownClassCount > 0 && (
        <p
          className="panel-note"
          style={{ padding: "10px 22px 0", color: "var(--warning)" }}
        >
          {formatCount(estimate.unknownClassCount)} document(s) carry a
          storage class the pricing snapshot does not list — they are
          charged at the Standard rate as a conservative fallback.
        </p>
      )}
      <p className="panel-note" style={{ padding: "16px 22px 18px", lineHeight: 1.6 }}>
        Estimate from live document metadata ({formatBytes(
          documents.reduce((sum, d) => sum + (d.file_size || 0), 0),
        )}{" "}
        raw footprint) at {PRICING_REGION} storage rates — optimizer pricing
        snapshot dated {PRICING_CHECKED}, AWS Price List API-validated.
        Snapshot storage charges only; retrieval, request and lifecycle
        charges are not estimated here. This is not an AWS invoice, and no
        savings are shown on live data.
      </p>
    </section>
  );
}

function SnapshotHead() {
  return (
    <div className="panel-head">
      <div>
        <div className="panel-title">Live storage cost estimate</div>
        <div className="panel-note">
          Client-side estimate from live metadata — {PRICING_SOURCE}
        </div>
      </div>
      <LiveBadge />
    </div>
  );
}