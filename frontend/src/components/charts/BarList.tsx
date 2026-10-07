// Horizontal grouped bar list — used for cost breakdowns
// (storage / retrieval / requests / transitions) where series
// share a single value axis, and for per-tier modeled costs on
// the document detail page.
//
// Follows the data-viz mark specs: bars <= 18px thick, 4px rounded
// data-end, square at the baseline, values direct-labeled after the
// bar tip, text in ink tokens, tooltips + table view.

import type { ReactNode } from "react";
import { useLayoutEffect, useRef } from "react";
import { ChartTip, TipRow, TipTitle, useChartTooltip } from "./ChartTooltip";

export interface BarListEntry {
  label: string;
  series: { value: number; color: string; name: string }[];
  /** Optional callout, rendered as a wrapping line under the row. */
  callout?: ReactNode;
}

const BAR_MAX_FRACTION = 0.72; // reserve the rest for the direct label

/** Direct label placed after the bar tip. Its left offset is measured
 * against the track at layout time and clamped so the text can never
 * overflow into the next card: when the tip sits too close to the
 * right edge, the label slides back over the bar and a surface-colored
 * halo keeps the ink legible on the colored fill underneath. */
function TipLabel({
  offsetPct,
  children,
}: {
  offsetPct: number;
  children: ReactNode;
}) {
  const ref = useRef<HTMLSpanElement>(null);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const fit = () => {
      const track = el.parentElement;
      if (!track) return;
      const trackW = track.clientWidth;
      if (trackW <= 0) return;
      const desiredPct = Math.min(offsetPct + 1.2, BAR_MAX_FRACTION * 100);
      const desired = Math.round((desiredPct / 100) * trackW) + 2;
      const w = el.offsetWidth;
      el.style.left = `${Math.max(0, Math.min(desired, trackW - w - 4))}px`;
    };
    fit();
    window.addEventListener("resize", fit);
    // Re-measure once webfonts settle: offsetWidth may change after load.
    document.fonts?.ready.then(() => fit()).catch(() => {});
    return () => window.removeEventListener("resize", fit);
  }, [offsetPct]);
  return (
    <span
      ref={ref}
      style={{
        position: "absolute",
        top: "50%",
        transform: "translateY(-50%)",
        fontSize: 11.5,
        color: "var(--text-2)",
        whiteSpace: "nowrap",
        fontVariantNumeric: "tabular-nums",
        textShadow: "0 1px 3px var(--panel), 0 0 6px var(--panel)",
      }}
    >
      {children}
    </span>
  );
}

export function BarList({
  entries,
  formatValue,
  maxOverride,
  ariaLabel,
  tableCaption,
}: {
  entries: BarListEntry[];
  formatValue: (value: number) => string;
  maxOverride?: number;
  ariaLabel: string;
  tableCaption?: string;
}) {
  const { containerRef, tip, show, hide } = useChartTooltip();

  const max =
    maxOverride ??
    Math.max(...entries.flatMap((e) => e.series.map((s) => s.value)), 0);

  const sharedTip = (entry: BarListEntry) => (
    <>
      <TipTitle>{entry.label}</TipTitle>
      {entry.series.map((t, k) => (
        <TipRow key={k} label={t.name} swatch={t.color} value={formatValue(t.value)} />
      ))}
      {entry.callout && (
        <div style={{ marginTop: 2, color: "var(--text-3)" }}>{entry.callout}</div>
      )}
    </>
  );

  return (
    <div ref={containerRef} className="chart-body" onPointerLeave={hide}>
      <div role="img" aria-label={ariaLabel}>
        {entries.map((entry) => {
          const longest = Math.max(...entry.series.map((s) => s.value));
          return (
            <div
              key={entry.label}
              style={{
                display: "grid",
                gridTemplateColumns: "minmax(84px, 110px) 1fr",
                columnGap: 12,
                rowGap: 3,
                alignItems: "center",
                padding: "8px 0",
                minWidth: 0,
              }}
            >
              <div
                style={{
                  fontSize: 12,
                  color: "var(--text-2)",
                  textAlign: "right",
                  whiteSpace: "nowrap",
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                }}
              >
                {entry.label}
              </div>
              <div style={{ position: "relative" }}>
                <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
                  {entry.series.map((s, j) => {
                    const fraction = max > 0 ? s.value / max : 0;
                    const width = fraction * BAR_MAX_FRACTION * 100;
                    return (
                      <div
                        key={j}
                        tabIndex={0}
                        role="listitem"
                        aria-label={`${entry.label} — ${s.name}: ${formatValue(s.value)}`}
                        onPointerMove={(e) => show(e, sharedTip(entry))}
                        onFocus={(e) => {
                          const rect = (
                            e.target as HTMLElement
                          ).getBoundingClientRect();
                          show(
                            {
                              clientX: rect.left + rect.width / 2,
                              clientY: rect.top,
                            },
                            sharedTip(entry),
                          );
                        }}
                        onBlur={hide}
                        style={{
                          width: s.value > 0 ? `${Math.max(width, 0.6)}%` : "0%",
                          minWidth: s.value > 0 ? 3 : 0,
                          height: entry.series.length === 1 ? 16 : 11,
                          background: s.color,
                          borderRadius:
                            j === 0
                              ? "0 2px 2px 0"
                              : "0 4px 4px 0",
                        }}
                      />
                    );
                  })}
                </div>
                <TipLabel
                  offsetPct={
                    max > 0 ? (longest / max) * BAR_MAX_FRACTION * 100 : 0
                  }
                >
                  {entry.series.length === 1
                    ? formatValue(entry.series[0].value)
                    : entry.series
                        .map((s) => formatValue(s.value))
                        .join("  →  ")}
                </TipLabel>
              </div>
              {entry.callout && (
                <div
                  style={{
                    gridColumn: 2,
                    fontSize: 11,
                    color: "var(--text-3)",
                  }}
                >
                  {entry.callout}
                </div>
              )}
            </div>
          );
        })}
      </div>
      <ChartTip tip={tip} />
      {tableCaption && (
        <details className="mt-2" style={{ padding: "0 2px 6px" }}>
          <summary
            style={{ cursor: "pointer", fontSize: 12, color: "var(--text-3)" }}
          >
            Table view
          </summary>
          <table
            className="data-table"
            style={{ fontSize: 12, width: "auto", marginTop: 8, minWidth: "60%" }}
          >
            <thead>
              <tr>
                <th>Category</th>
                {entries[0]?.series.map((s, i) => (
                  <th key={i}>{s.name}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {entries.map((entry) => (
                <tr key={entry.label}>
                  <td>{entry.label}</td>
                  {entry.series.map((s, j) => (
                    <td key={j} className="num">
                      {formatValue(s.value)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      )}
    </div>
  );
}