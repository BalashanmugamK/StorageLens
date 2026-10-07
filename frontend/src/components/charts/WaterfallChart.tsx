// Waterfall chart — how the baseline annual cost decomposes into the
// optimizer's per-component changes. One start bar at [0, baseline],
// floating delta bars that rise from or fall to the running level
// (negative = cost removed = savings), one end bar at [0, final].
//
// Follows the chart conventions shared with ColumnHistogram: SVG,
// fixed plot height, hairline grid with clean ticks, 4px rounded
// data-end only (square at the running-level junction), 2px gaps not
// needed here because bars don't overlap, dashed running-level
// connectors, tooltips on pointer and focus, table view fallback.
// Color follows meaning: baseline = --series-current, savings =
// --div-pos, added cost = --div-neg, total = --series-optimized.
// Text always wears ink tokens, never series color.

import { useLayoutEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { ChartTable, ChartTip, TipRow, TipTitle, useChartTooltip } from "./ChartTooltip";

export interface WaterfallStep {
  label: string;
  /** Signed change in annual cost; negative = savings. */
  value: number;
  kind: "start" | "delta" | "end";
  /** Neutral reconciliation bar (arithmetic drift) uses --div-mid. */
  neutral?: boolean;
  /** Optional callout rendered inside the tooltip. */
  note?: ReactNode;
}

const PLOT_HEIGHT = 180;
const BAR_MAX_WIDTH = 34;
const LEFT_AXIS_ROOM = 56;
const BOTTOM_ROOM = 40;

const STEP_COLOR: Record<WaterfallStep["kind"] | "neutral", string> = {
  start: "var(--series-current)",
  delta: "var(--div-pos)",
  end: "var(--series-optimized)",
  neutral: "var(--div-mid)",
};

interface Bar {
  step: WaterfallStep;
  index: number;
  from: number;
  to: number;
  total: number;
}

const LEGEND_ITEMS = [
  { key: "start", label: "Baseline" },
  { key: "delta", label: "Savings" },
  { key: "added", label: "Added cost", color: "var(--div-neg)" },
  { key: "neutral", label: "Reconciliation", color: "var(--div-mid)" },
  { key: "end", label: "Optimizer" },
] as const;

/** Path with the rounded end at the bar's data end and a square
 * junction edge. When `roundTop` the rounding is at the top edge,
 * else at the bottom edge. */
function barPath(x: number, y: number, w: number, h: number, r: number, roundTop: boolean): string {
  const rr = Math.min(r, w / 2, h);
  if (roundTop) {
    return [
      `M ${x} ${y + h}`,
      `V ${y + rr}`,
      `Q ${x} ${y} ${x + rr} ${y}`,
      `H ${x + w - rr}`,
      `Q ${x + w} ${y} ${x + w} ${y + rr}`,
      `V ${y + h}`,
      "Z",
    ].join(" ");
  }
  return [
    `M ${x} ${y}`,
    `H ${x + w}`,
    `V ${y + h - rr}`,
    `Q ${x + w} ${y + h} ${x + w - rr} ${y + h}`,
    `H ${x + rr}`,
    `Q ${x} ${y + h} ${x} ${y + h - rr}`,
    "Z",
  ].join(" ");
}

/** Even ticks across [lo, hi] including both ends and 0. */
function rangeTicks(lo: number, hi: number): number[] {
  const span = hi - lo;
  if (span <= 0) return [lo, hi];
  const raw = span / 4;
  const magnitude = 10 ** Math.floor(Math.log10(Math.abs(raw)));
  const step =
    [1, 2, 5, 10].map((m) => m * magnitude).find((s) => s >= raw) ??
    10 * magnitude;
  const first = Math.floor(lo / step) * step;
  const last = Math.ceil(hi / step) * step;
  const ticks: number[] = [];
  for (let t = first; t <= last + step / 2; t += step) ticks.push(t);
  return ticks;
}

export function WaterfallChart({
  steps,
  formatValue,
  ariaLabel,
  tableCaption,
}: {
  steps: WaterfallStep[];
  formatValue: (value: number) => string;
  ariaLabel: string;
  tableCaption?: string;
}) {
  const { containerRef, tip, show, hide } = useChartTooltip();
  // Content width available for the plot: the chart should fill its
  // panel rather than stop at the step-count estimate — measured
  // (.chart-body padding included) so bars spread across the real
  // column at any panel size. The steps-based minimum stays the
  // first-paint fallback until the layout effect measures.
  const [contentW, setContentW] = useState(0);
  useLayoutEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const measure = () => {
      const cs = getComputedStyle(el);
      const padX =
        (parseFloat(cs.paddingLeft) || 0) + (parseFloat(cs.paddingRight) || 0);
      setContentW(Math.max(0, el.clientWidth - padX));
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
    // containerRef is a stable ref from useChartTooltip.
  }, [containerRef]);

  // Walk the running total across the steps: start sets it, deltas
  // move it, end pins it (the end bar is drawn from zero). The walk
  // is a pure reduce over a local accumulator so nothing captured by
  // the render closure is reassigned.
  const bars = useMemo(
    () =>
      steps
        .reduce<{ list: Bar[]; running: number }>(
          (acc, step, index) => {
            if (step.kind === "start") {
              acc.running = step.value;
              acc.list.push({ step, index, from: 0, to: step.value, total: step.value });
            } else if (step.kind === "end") {
              acc.running = step.value;
              acc.list.push({ step, index, from: 0, to: step.value, total: step.value });
            } else {
              const from = acc.running;
              const to = acc.running + step.value;
              acc.running = to;
              acc.list.push({ step, index, from, to, total: to });
            }
            return acc;
          },
          { list: [], running: 0 },
        )
        .list,
    [steps],
  );

  const levels = bars.flatMap((b) => [b.from, b.to]);
  const lo = Math.min(...levels, 0);
  const hi = Math.max(...levels, 0);
  const ticks = useMemo(() => rangeTicks(lo, hi), [lo, hi]);

  const plotWidth = Math.max(
    steps.length * 64,
    280,
    contentW - LEFT_AXIS_ROOM,
  );
  const width = plotWidth + LEFT_AXIS_ROOM;
  const plotTop = 8;
  const plotBottom = plotTop + PLOT_HEIGHT;
  const domainLo = ticks[0];
  const domainHi = ticks[ticks.length - 1];
  const yFor = (value: number) =>
    plotBottom - ((value - domainLo) / (domainHi - domainLo || 1)) * PLOT_HEIGHT;

  const slot = (plotWidth - 16) / steps.length;
  const barWidth = Math.min(BAR_MAX_WIDTH, slot * 0.7);
  const xFor = (index: number) =>
    LEFT_AXIS_ROOM + 8 + index * slot + (slot - barWidth) / 2;

  const presentKinds = new Set(
    bars.map((b) => (b.step.neutral ? "neutral" : b.step.kind)),
  );
  const legend = LEGEND_ITEMS.filter((item) => {
    if (item.key === "added") return steps.some((s) => s.kind === "delta" && !s.neutral && s.value > 0);
    if (item.key === "delta") return steps.some((s) => s.kind === "delta" && !s.neutral && s.value < 0);
    return presentKinds.has(item.key);
  });

  const barTitle = (bar: (typeof bars)[number]) => {
    const step = bar.step;
    if (step.kind === "start") return "Baseline annual cost";
    if (step.kind === "end") return "Optimizer annual cost";
    return step.value < 0 ? "Cost reduction" : "Added cost";
  };

  return (
    <div ref={containerRef} onPointerLeave={hide} className="chart-body">
      <div className="legend" style={{ padding: "0 0 14px" }}>
        {legend.map((item) => (
          <span className="legend-item" key={item.key}>
            <span className="legend-swatch" style={{ background: "color" in item ? item.color : STEP_COLOR[item.key] }} />
            {item.label}
          </span>
        ))}
      </div>
      <svg
        role="img"
        aria-label={ariaLabel}
        width="100%"
        viewBox={`0 0 ${width} ${plotBottom + BOTTOM_ROOM}`}
        style={{ display: "block" }}
        preserveAspectRatio="xMidYMid meet"
      >
        {/* hairline grid + tick labels */}
        {ticks.map((t) => (
          <g key={t}>
            <line
              className="grid-line"
              x1={LEFT_AXIS_ROOM}
              x2={width - 4}
              y1={yFor(t)}
              y2={yFor(t)}
            />
            <text
              className="axis-text tnum"
              x={LEFT_AXIS_ROOM - 8}
              y={yFor(t) + 3.5}
              textAnchor="end"
            >
              {formatValue(t)}
            </text>
          </g>
        ))}
        {bars.map((bar) => {
          const step = bar.step;
          // Color follows sign, not just kind: a positive delta is an
          // added cost (--div-neg, matching the legend's red chip);
          // negative deltas are savings, neutral stays --div-mid.
          const color =
            step.kind === "delta"
              ? step.neutral
                ? STEP_COLOR.neutral
                : step.value > 0
                  ? "var(--div-neg)"
                  : STEP_COLOR.delta
              : STEP_COLOR[step.kind];
          const x = xFor(bar.index);
          const top = Math.min(yFor(bar.from), yFor(bar.to));
          const bottom = Math.max(yFor(bar.from), yFor(bar.to));
          const h = Math.max(bottom - top, bar.step.kind === "delta" ? 2 : 1);
          const valueLabel =
            step.kind !== "delta" ? formatValue(step.value) : null;
          const isFloating = step.kind === "delta";
          const isVisible =
            !isFloating || Math.abs(step.value) >= 0.005;
          const connector = bars[bar.index + 1];
          return (
            <g key={`${step.label}-${bar.index}`}>
              {bar.index < bars.length - 1 && connector && (
                <line
                  className="grid-line"
                  stroke="var(--grid)"
                  strokeWidth={1}
                  strokeDasharray="3 3"
                  x1={x + barWidth}
                  x2={xFor(connector.index)}
                  y1={yFor(isFloating ? bar.to : step.value)}
                  y2={yFor(isFloating ? bar.to : step.value)}
                />
              )}
              {isVisible && (
                <path
                  d={barPath(x, top, barWidth, h, 4, !isFloating || bar.to >= bar.from)}
                  fill={color}
                  tabIndex={0}
                  role="listitem"
                  aria-label={`${step.label}: ${barTitle(bar)}, running total ${formatValue(bar.total)}`}
                  onPointerMove={(e) =>
                    show(
                      e,
                      <>
                        <TipTitle>{step.label}</TipTitle>
                        <TipRow
                          label={barTitle(bar)}
                          value={
                            step.kind === "delta"
                              ? `${step.value < 0 ? "−" : "+"}${formatValue(Math.abs(step.value))}`
                              : formatValue(step.value)
                          }
                          swatch={color}
                        />
                        <TipRow label="Running annual" value={formatValue(bar.total)} />
                        {step.note}
                      </>,
                    )
                  }
                  onFocus={(e) => {
                    const rect = (e.currentTarget as SVGGraphicsElement).getBoundingClientRect();
                    show(
                      { clientX: rect.left + rect.width / 2, clientY: rect.top },
                      <>
                        <TipTitle>{step.label}</TipTitle>
                        <TipRow
                          label={barTitle(bar)}
                          value={
                            step.kind === "delta"
                              ? `${step.value < 0 ? "−" : "+"}${formatValue(Math.abs(step.value))}`
                              : formatValue(step.value)
                          }
                          swatch={color}
                        />
                        <TipRow label="Running annual" value={formatValue(bar.total)} />
                        {step.note}
                      </>,
                    );
                  }}
                  onBlur={hide}
                />
              )}
              {valueLabel && (
                <text
                  className="axis-text tnum"
                  x={x + barWidth / 2}
                  y={top - 6}
                  textAnchor="middle"
                >
                  {valueLabel}
                </text>
              )}
              <text
                className="axis-text"
                x={x + barWidth / 2}
                y={plotBottom + 16}
                textAnchor="middle"
              >
                {step.label}
              </text>
            </g>
          );
        })}
        {/* baseline */}
        <line
          stroke="var(--axis)"
          strokeWidth={1}
          x1={LEFT_AXIS_ROOM}
          x2={width - 4}
          y1={yFor(0)}
          y2={yFor(0)}
        />
      </svg>
      <ChartTip tip={tip} />
      {tableCaption && (
        <ChartTable
          caption={tableCaption}
          head={["Step", "Change", "Running annual"]}
          rows={bars.map((bar) => [
            bar.step.label,
            bar.step.kind === "delta"
              ? `${bar.step.value < 0 ? "−" : "+"}${formatValue(Math.abs(bar.step.value))}`
              : formatValue(bar.step.value),
            formatValue(bar.total),
          ])}
        />
      )}
    </div>
  );
}

export interface DeltaEntry {
  label: string;
  /** Signed change in annual cost; negative = savings (modeled). */
  value: number;
  /** Neutral reconciliation bar (arithmetic drift) uses --div-mid. */
  neutral?: boolean;
  /** Optional extra rows inside the tooltip. */
  note?: ReactNode;
}

/** Normal anchored signed bar chart for component changes: every bar
 * starts on the SAME $0 axis — savings extend below it, added cost
 * rises above it — instead of the waterfall's floating deltas. Each
 * bar carries its value as a direct label and a tooltip with the
 * running annual total (baseline + changes so far). */
export function DeltaColumns({
  deltas,
  baselineTotal,
  formatValue,
  ariaLabel,
  tableCaption,
}: {
  deltas: DeltaEntry[];
  baselineTotal: number;
  formatValue: (value: number) => string;
  ariaLabel: string;
  tableCaption?: string;
}) {
  const { containerRef, tip, show, hide } = useChartTooltip();

  // Same panel-fill measurement as WaterfallChart / ColumnHistogram.
  const [contentW, setContentW] = useState(0);
  useLayoutEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const measure = () => {
      const cs = getComputedStyle(el);
      const padX =
        (parseFloat(cs.paddingLeft) || 0) + (parseFloat(cs.paddingRight) || 0);
      setContentW(Math.max(0, el.clientWidth - padX));
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
    // containerRef is a stable ref from useChartTooltip.
  }, [containerRef]);

  const PLOT_HEIGHT_LOCAL = 190;
  const PLOT_TOP_LOCAL = 14;
  const BOTTOM_ROOM_LOCAL = 44;
  const LEFT_ROOM = 56;

  const lo = Math.min(...deltas.map((d) => d.value), 0);
  const hi = Math.max(...deltas.map((d) => d.value), 1);
  const ticks = useMemo(() => rangeTicks(lo, hi), [lo, hi]);

  const plotWidth = Math.max(
    deltas.length * 72,
    280,
    contentW - LEFT_ROOM,
  );
  const width = plotWidth + LEFT_ROOM;
  const plotTop = PLOT_TOP_LOCAL;
  const plotBottom = plotTop + PLOT_HEIGHT_LOCAL;
  const domainLo = ticks[0];
  const domainHi = ticks[ticks.length - 1];
  const yFor = (value: number) =>
    plotBottom - ((value - domainLo) / (domainHi - domainLo || 1)) * PLOT_HEIGHT_LOCAL;

  const slot = (plotWidth - 16) / deltas.length;
  const barWidth = Math.min(BAR_MAX_WIDTH, slot * 0.7);
  const xFor = (index: number) =>
    LEFT_ROOM + 8 + index * slot + (slot - barWidth) / 2;

  // Running annual walks baseline + the changes in row order, exactly
  // like the waterfall's reconciliation — kept so hover/table stay
  // verifiable against totals.
  const walk: number[] = [];
  deltas.reduce((running, d) => (walk.push(running + d.value), running + d.value), baselineTotal);

  const hasSavings = deltas.some((d) => d.value < 0 && !d.neutral);
  const hasAdded = deltas.some((d) => d.value > 0 && !d.neutral);
  const neutralNote = deltas.some((d) => d.neutral);

  return (
    <div ref={containerRef} onPointerLeave={hide} className="chart-body">
      <div className="legend" style={{ padding: "0 0 14px" }}>
        {hasSavings && (
          <span className="legend-item">
            <span className="legend-swatch" style={{ background: "var(--div-pos)" }} />
            Savings
          </span>
        )}
        {hasAdded && (
          <span className="legend-item">
            <span className="legend-swatch" style={{ background: "var(--div-neg)" }} />
            Added cost
          </span>
        )}
        {neutralNote && (
          <span className="legend-item">
            <span className="legend-swatch" style={{ background: "var(--div-mid)" }} />
            Reconciliation
          </span>
        )}
      </div>
      <svg
        role="img"
        aria-label={ariaLabel}
        width="100%"
        viewBox={`0 0 ${width} ${plotBottom + BOTTOM_ROOM_LOCAL}`}
        style={{ display: "block" }}
        preserveAspectRatio="xMidYMid meet"
      >
        {/* hairline grid + tick labels */}
        {ticks.map((t) => (
          <g key={t}>
            <line
              className="grid-line"
              x1={LEFT_ROOM}
              x2={width - 4}
              y1={yFor(t)}
              y2={yFor(t)}
            />
            <text
              className="axis-text tnum"
              x={LEFT_ROOM - 8}
              y={yFor(t) + 3.5}
              textAnchor="end"
            >
              {formatValue(t)}
            </text>
          </g>
        ))}
        {deltas.map((delta, index) => {
          const color = delta.neutral
            ? "var(--div-mid)"
            : delta.value < 0
              ? "var(--div-pos)"
              : "var(--div-neg)";
          const x = xFor(index);
          const zero = yFor(0);
          const valueY = yFor(delta.value);
          const top = Math.min(zero, valueY);
          const bottom = Math.max(zero, valueY);
          // Savings bars extend below the axis (data end = bottom,
          // rounded there); added cost rises above (rounded at top).
          const h = Math.max(bottom - top, 2);
          const roundBottom = delta.value < 0;
          const running = walk[index];
          return (
            <g key={`${delta.label}-${index}`}>
              <path
                d={
                  roundBottom
                    ? barPath(x, top, barWidth, h, 4, false)
                    : barPath(x, top, barWidth, h, 4, true)
                }
                fill={color}
                tabIndex={0}
                role="listitem"
                aria-label={`${delta.label}: modeled change ${delta.value < 0 ? "−" : "+"}${formatValue(Math.abs(delta.value))}, running annual ${formatValue(running)}`}
                onPointerMove={(e) =>
                  show(
                    e,
                    <>
                      <TipTitle>{delta.label}</TipTitle>
                      <TipRow
                        label={barTitleFor(delta)}
                        value={`${delta.value < 0 ? "−" : "+"}${formatValue(Math.abs(delta.value))}`}
                        swatch={color}
                      />
                      <TipRow label="Running annual" value={formatValue(running)} />
                      {delta.note}
                    </>,
                  )
                }
                onFocus={(e) => {
                  const rect = (e.currentTarget as SVGGraphicsElement).getBoundingClientRect();
                  show(
                    { clientX: rect.left + rect.width / 2, clientY: rect.top },
                    <>
                      <TipTitle>{delta.label}</TipTitle>
                      <TipRow
                        label={barTitleFor(delta)}
                        value={`${delta.value < 0 ? "−" : "+"}${formatValue(Math.abs(delta.value))}`}
                        swatch={color}
                      />
                      <TipRow label="Running annual" value={formatValue(running)} />
                      {delta.note}
                    </>,
                  );
                }}
                onBlur={hide}
              />
              <text
                className="axis-text tnum"
                x={x + barWidth / 2}
                y={roundBottom ? bottom + 13 : top - 6}
                textAnchor="middle"
              >
                {`${delta.value < 0 ? "−" : "+"}${formatValue(Math.abs(delta.value))}`}
              </text>
              <text
                className="axis-text"
                x={x + barWidth / 2}
                y={plotBottom + 16}
                textAnchor="middle"
              >
                {delta.label}
              </text>
            </g>
          );
        })}
        {/* the shared zero axis every bar starts from */}
        <line
          stroke="var(--axis)"
          strokeWidth={1}
          x1={LEFT_ROOM}
          x2={width - 4}
          y1={yFor(0)}
          y2={yFor(0)}
        />
      </svg>
      <ChartTip tip={tip} />
      {tableCaption && (
        <ChartTable
          caption={tableCaption}
          head={["Component", "Change", "Running annual"]}
          rows={deltas.map((delta, index) => [
            delta.label,
            `${delta.value < 0 ? "−" : "+"}${formatValue(Math.abs(delta.value))}`,
            formatValue(walk[index]),
          ])}
        />
      )}
    </div>
  );
}

function barTitleFor(delta: DeltaEntry): string {
  if (delta.neutral) return "Reconciliation";
  return delta.value < 0 ? "Cost reduction (modeled)" : "Added cost";
}