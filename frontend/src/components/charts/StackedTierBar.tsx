// Tier-distribution visualization. Two stacked horizontal bars —
// Current allocation vs Recommended allocation — each segmented by
// storage tier on an ordinal hue ramp (hot → cold), with a 2px
// surface gap between segments and in-segment labels only when they
// fit. Tooltips carry the tier's document count, storage and cost.

import type { ReactNode } from "react";
import { tierIndex } from "../ui/atoms";
import { ChartTable, ChartTip, TipRow, TipTitle, useChartTooltip } from "./ChartTooltip";

export interface TierSegment {
  tier: string;
  tierLabel: string;
  color: string;
  documentCount: number;
  storageGb?: number;
  annualCost?: number;
}

function StackedRow({
  name,
  segments,
  total,
  onHover,
  onLeave,
}: {
  name: string;
  segments: TierSegment[];
  total: number;
  onHover: (event: { clientX: number; clientY: number }, tip: ReactNode) => void;
  onLeave: () => void;
}) {
  return (
    <div>
      <div style={{ fontSize: 12, color: "var(--text-3)", marginBottom: 6, fontWeight: 550 }}>
        {name}
        <span className="muted tnum" style={{ float: "right" }}>
          {total.toLocaleString()} documents
        </span>
      </div>
      <div
        style={{
          display: "flex",
          gap: 2,
          height: 24,
          overflow: "hidden",
          borderRadius: 4,
        }}
      >
        {segments.map((seg) => {
          const fraction = total > 0 ? seg.documentCount / total : 0;
          const fits = fraction >= 0.085;
          return (
            <div
              key={seg.tier}
              tabIndex={0}
              role="listitem"
              aria-label={`${name}, ${seg.tierLabel}: ${seg.documentCount} documents${
                seg.annualCost !== undefined
                  ? `, annual cost ${seg.annualCost.toFixed(2)} dollars`
                  : ""
              }`}
              onPointerMove={(e) =>
                onHover(
                  e,
                  <>
                    <TipTitle>{seg.tierLabel}</TipTitle>
                    <TipRow label="Documents" value={seg.documentCount.toLocaleString()} swatch={seg.color} />
                    {seg.storageGb !== undefined && (
                      <TipRow label="Storage" value={`${seg.storageGb.toFixed(1)} GB`} swatch={seg.color} />
                    )}
                    {seg.annualCost !== undefined && (
                      <TipRow label="Projected annual" value={`$${seg.annualCost.toFixed(2)}`} swatch={seg.color} />
                    )}
                  </>,
                )
              }
              onPointerLeave={onLeave}
              onFocus={(e) => {
                const rect = (e.target as HTMLElement).getBoundingClientRect();
                onHover(
                  { clientX: rect.left + rect.width / 2, clientY: rect.top },
                  <>
                    <TipTitle>{seg.tierLabel}</TipTitle>
                    <TipRow label="Documents" value={seg.documentCount.toLocaleString()} />
                    {seg.storageGb !== undefined && (
                      <TipRow label="Storage" value={`${seg.storageGb.toFixed(1)} GB`} />
                    )}
                  </>,
                );
              }}
              onBlur={onLeave}
              style={{
                width: seg.documentCount > 0 ? `${Math.max(fraction * 100, 0.5)}%` : "0%",
                background: seg.color,
                minWidth: seg.documentCount > 0 ? 2 : 0,
              }}
            >
              {fits && (
                <span
                  style={{
                    display: "block",
                    padding: "3.5px 7px",
                    fontSize: 10.5,
                    fontWeight: 600,
                    whiteSpace: "nowrap",
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                    color: `var(--tier-ink-${tierIndex(seg.tier) + 1})`,
                  }}
                >
                  {seg.tierLabel}
                  {seg.storageGb !== undefined && (
                    <span style={{ opacity: 0.75, fontWeight: 500 }}>
                      {" "}
                      · {seg.storageGb.toFixed(1)} GB
                    </span>
                  )}
                </span>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

export function StackedTierBar({
  current,
  recommended,
  ariaLabel,
  tableCaption,
}: {
  current: TierSegment[];
  recommended?: TierSegment[];
  ariaLabel: string;
  tableCaption?: string;
}) {
  const { containerRef, tip, show, hide } = useChartTooltip();

  const currentTotal = current.reduce((s, x) => s + x.documentCount, 0);
  const recommendedTotal = recommended?.reduce((s, x) => s + x.documentCount, 0) ?? 0;

  const legend = recommended ?? current;

  const tableRows: ReactNode[][] =
    recommended === undefined
      ? [
          ["Current", ...current.map((s) => `${s.tierLabel} · ${s.documentCount.toLocaleString()}`)],
        ]
      : [
          ["Current", ...current.map((s) => `${s.tierLabel}: ${s.documentCount.toLocaleString()}`)],
          ["Recommended", ...recommended.map((s) => `${s.tierLabel}: ${s.documentCount.toLocaleString()}`)],
        ];

  return (
    <div
      ref={containerRef}
      onPointerLeave={hide}
      className="chart-body"
      style={{ padding: "12px 22px 18px" }}
    >
      <div role="img" aria-label={ariaLabel}>
        <div className="legend" style={{ padding: "0 0 16px" }}>
          {legend.map((seg) => (
            <span className="legend-item" key={seg.tier}>
              <span className="legend-swatch" style={{ background: seg.color }} />
              {seg.tierLabel}
            </span>
          ))}
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <StackedRow
            name="Current allocation"
            segments={current}
            total={currentTotal}
            onHover={show}
            onLeave={hide}
          />
          {recommended && (
            <StackedRow
              name="Recommended allocation"
              segments={recommended}
              total={recommendedTotal}
              onHover={show}
              onLeave={hide}
            />
          )}
        </div>
      </div>
      <ChartTip tip={tip} />
      {tableCaption && (
        <ChartTable caption={tableCaption} head={["Allocation", "Segments"]} rows={tableRows} />
      )}
    </div>
  );
}