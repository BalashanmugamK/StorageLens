// Reconciliation — the model vs the actual bill. The panel pulls the
// latest recorded report (404 = never run) and can trigger a run
// against live Cost Explorer data.
//
// Every number is shown with its source: actual = Cost Explorer S3
// usage-type charges for complete months; predicted = the engine's
// storage component over the live aggregate snapshot; variance =
// predicted − actual. Requests/transfer and unmapped storage line
// items are visible, never silently dropped or folded in.

import { useCallback, useEffect, useState } from "react";
import { Scale } from "lucide-react";
import { api, ApiError } from "../../services/api";
import type { ApiReconciliationMonth, ApiReconciliationReport } from "../../types";
import {
  EmptyState,
  Spinner,
  StatTile,
} from "../ui/atoms";
import { formatMoney, tierLabel } from "../../utils/format";

// Buckets that are real charges but outside the model's storage
// prediction — they render with their own labels.
const NON_MODEL_BUCKETS: Record<string, string> = {
  requests_and_transfer: "Requests + transfer + other",
  unmapped_storage: "Unmapped storage (not modelled)",
};

function bucketLabel(bucket: string): string {
  if (NON_MODEL_BUCKETS[bucket]) return NON_MODEL_BUCKETS[bucket];
  if (bucket === "INTELLIGENT_TIERING") return tierLabel(bucket);
  return tierLabel(bucket);
}

// Cell display for one (month, bucket): actual on top, the model's
// prediction beneath it. Actual-only rows (non-model) skip the
// second line entirely — nothing was predicted, and showing a
// fabricated 0 would imply the model priced it.
function Cell({
  month,
  bucket,
  label,
}: {
  month: ApiReconciliationMonth;
  bucket: string;
  label: "actual" | "variance";
}) {
  const actual = month.actual_usd[bucket];
  const other =
    label === "actual"
      ? month.predicted_usd[bucket]
      : month.variance_usd[bucket];

  const predicted = bucket in month.predicted_usd;

  return (
    <td className="num tnum" style={{ verticalAlign: "top" }}>
      <span title={label === "actual" ? "Actual (Cost Explorer)" : "Predicted − actual"}>
        {label === "actual" ? formatMoney(actual ?? 0) : formatMoney(other ?? 0)}
      </span>
      {predicted && label === "actual" && (
        <div
          className="doc-sub tnum"
          title="Predicted by the engine over the live aggregate snapshot"
        >
          pred {formatMoney(month.predicted_usd[bucket])}
        </div>
      )}
      {predicted && label === "variance" && (
        <div className="doc-sub tnum" title="Actual (Cost Explorer)">
          act {formatMoney(month.actual_usd[bucket] ?? 0)}
        </div>
      )}
    </td>
  );
}

export function ReconciliationPanel() {
  const [report, setReport] = useState<ApiReconciliationReport | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "not-run">("loading");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const result = await api.getReconciliationResult();
      setReport(result);
      setState("ready");
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        setState("not-run");
      } else {
        setError(
          err instanceof Error ? err.message : "Failed to load the report",
        );
      }
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const run = async () => {
    setBusy(true);
    setError(null);
    try {
      const result = await api.runReconciliation();
      setReport(result);
      setState("ready");
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "The reconciliation could not be run.",
      );
    } finally {
      setBusy(false);
    }
  };

  if (state === "loading") {
    return (
      <div style={{ padding: 16 }}>
        <Spinner />
      </div>
    );
  }

  if (state === "not-run") {
    return (
      <EmptyState
        icon={<Scale size={18} />}
        title="The model has never been checked against the bill"
        description="Run the reconciliation: the backend compares complete months of actual S3 charges (Cost Explorer) with the engine's prediction over the live fleet, and records the variance. A daily run keeps this current without anyone remembering."
        actions={
          <button
            type="button"
            className="btn btn-primary"
            onClick={run}
            disabled={busy}
          >
            {busy ? "Reconciling…" : "Run reconciliation"}
          </button>
        }
      />
    );
  }

  if (!report) return null;

  // Buckets union across months, in tier order with non-model last.
  const buckets = [
    ...new Set(
      report.months.flatMap((month) => [
        ...Object.keys(month.actual_usd),
        ...Object.keys(month.predicted_usd),
      ]),
    ),
  ];
  buckets.sort((a, b) => {
    const rank = (key: string) =>
      NON_MODEL_BUCKETS[key] ? 2 : 1;
    return (
      rank(a) - rank(b) ||
      bucketLabel(a).localeCompare(bucketLabel(b))
    );
  });

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
          label="Variance (predicted − actual)"
          value={formatMoney(report.totals.variance_usd)}
          footer={
            <span className="muted" style={{ fontSize: 11 }}>
              positive: model expects more spend than the bill shows
            </span>
          }
        />
        <StatTile
          label="Actual storage / window"
          value={formatMoney(report.totals.actual_storage_usd)}
          footer={
            <span className="muted" style={{ fontSize: 11 }}>
              Cost Explorer, {report.months_requested} complete month
              {report.months_requested === 1 ? "" : "s"}
            </span>
          }
        />
        <StatTile
          label="Predicted storage / window"
          value={formatMoney(report.totals.predicted_storage_usd)}
          footer={
            <span className="muted" style={{ fontSize: 11 }}>
              engine over the live aggregate snapshot
            </span>
          }
        />
        <StatTile
          label="Non-model charges"
          value={formatMoney(report.totals.actual_non_storage_usd)}
          footer={
            <span className="muted" style={{ fontSize: 11 }}>
              requests, transfer, unmapped — real, not predicted
            </span>
          }
        />
      </div>

      {report.months.length === 0 ? (
        <EmptyState
          title="No complete months of billing data yet"
          description="Cost Explorer reports complete months only; the reconciliation will have rows once the account has at least one."
        />
      ) : (
        <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr>
                <th>Month</th>
                <th>Line item</th>
                <th className="num" title="Actual monthly cost from Cost Explorer">
                  Actual
                </th>
                <th className="num" title="Predicted − actual; positive means the model expects more spend than the bill">
                  Variance
                </th>
              </tr>
            </thead>
            <tbody>
              {report.months.map((month) => (
                <MonthRows key={month.month} month={month} buckets={buckets} />
              ))}
            </tbody>
          </table>
        </div>
      )}

      <p
        className="muted"
        style={{ fontSize: 11.5, margin: "12px 0 4px", lineHeight: 1.6 }}
      >
        Basis: {report.basis.replace(/[.\s]+$/, "")}. The ledger's claim so far —{" "}
        {report.ledger.verified_decisions} verified transition
        {report.ledger.verified_decisions === 1 ? "" : "s"},{" "}
        {formatMoney(report.ledger.realized_savings_annual)}/yr realized — sits
        beside these figures; the report deliberately does not attribute a
        bill delta to individual transitions.
        {report.prediction_failures > 0 && (
          <> {report.prediction_failures} aggregate
          {report.prediction_failures === 1 ? "" : "s"} could not be priced and
          are counted, not hidden.</>
        )}
      </p>

      <div className="row" style={{ gap: 8, marginTop: 10 }}>
        <button
          type="button"
          className="btn"
          onClick={run}
          disabled={busy}
          title="Re-run against live Cost Explorer data (complete months only)"
        >
          {busy ? "Reconciling…" : "Re-run reconciliation"}
        </button>
        <button
          type="button"
          className="btn btn-ghost"
          onClick={load}
          disabled={busy}
        >
          Reload report
        </button>
        <span className="muted" style={{ fontSize: 11 }}>
          runs daily on the schedule too · last run {new Date(report.ran_at).toLocaleString()}
        </span>
      </div>

      {error && (
        <p style={{ fontSize: 12.5, color: "var(--danger, #c0392b)", marginTop: 8 }}>
          {error}
        </p>
      )}
    </div>
  );
}

function MonthRows({
  month,
  buckets,
}: {
  month: ApiReconciliationMonth;
  buckets: string[];
}) {
  // Only line items this month actually billed or the model
  // predicted — empty columns stay empty rather than showing rows
  // of zeros.
  const present = buckets.filter(
    (bucket) =>
      bucket === "unmapped_storage" || bucket in month.actual_usd || bucket in month.predicted_usd,
  );

  const rows = present.length > 0 ? present : ["STANDARD"];

  return rows.map((bucket, index) => (
    <tr key={`${month.month}-${bucket}`}>
      {index === 0 ? (
        <td rowSpan={rows.length}>
          <span className="tnum">{month.month.slice(0, 7)}</span>
          {month.estimated_month && (
            <div
              className="doc-sub"
              title="Cost Explorer marks this month as an estimate (data lags up to ~24 h)"
            >
              estimated
            </div>
          )}
        </td>
      ) : null}
      <td>{bucketLabel(bucket)}</td>
      <Cell month={month} bucket={bucket} label="actual" />
      <Cell month={month} bucket={bucket} label="variance" />
    </tr>
  ));
}