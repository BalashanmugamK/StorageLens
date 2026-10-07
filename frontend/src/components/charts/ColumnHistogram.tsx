// Vertical column histogram — used for distributions (verdict mix,
// savings bands, access bands) where each column is one category.
// Ordinal categories may carry a one-hue ordinal ramp; polarity
// distributions carry the diverging pair. Columns <= 24px wide,
// 4px rounded top, square baseline, hairline y-grid with clean
// ticks, tooltips, table view.

import { useId, useLayoutEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { ChartTable, ChartTip, TipRow, TipTitle, useChartTooltip } from "./ChartTooltip";

export interface HistogramBin {
  label: string;
  value: number;
  color?: string;
  tooltip?: ReactNode;
}

const PLOT_HEIGHT = 168;
const BAR_MAX_WIDTH = 26;
const LEFT_AXIS_ROOM = 44;
const BOTTOM_ROOM = 34;

/** Column path with a rounded data-end (top) and a square baseline. */
function columnPath(x: number, y: number, w: number, h: number, r: number): string {
  const rr = Math.min(r, w / 2, h);
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

function niceTicks(maxValue: number): number[] {
  if (maxValue <= 0) return [0, 1];
  const raw = maxValue / 4;
  const magnitude = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 5, 10].map((m) => m * magnitude).find((s) => s >= raw) ?? 10 * magnitude;
  const top = Math.ceil(maxValue / step) * step;
  const ticks: number[] = [];
  for (let t = 0; t <= top; t += step) ticks.push(t);
  return ticks;
}

export function ColumnHistogram({
  bins,
  formatValue,
  ariaLabel,
  tableCaption,
  tooltipTitle,
}: {
  bins: HistogramBin[];
  formatValue: (value: number) => string;
  ariaLabel: string;
  tableCaption?: string;
  tooltipTitle?: (bin: HistogramBin) => ReactNode;
}) {
  const { containerRef, tip, show, hide } = useChartTooltip();
  const gradientId = useId().replace(/[:]/g, "");

  // Fill the panel's real width (measured minus .chart-body padding);
  // the bins-based minimum is the first-paint fallback. Same pattern as
  // WaterfallChart.
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

  const maxValue = Math.max(...bins.map((b) => b.value), 1);
  const ticks = useMemo(() => niceTicks(maxValue), [maxValue]);
  const yMax = ticks[ticks.length - 1];

  const plotWidth = Math.max(bins.length * 64, 280, contentW - LEFT_AXIS_ROOM);
  const width = plotWidth + LEFT_AXIS_ROOM;

  const plotTop = 6;
  const plotBottom = plotTop + PLOT_HEIGHT;

  const yFor = (value: number) =>
    plotBottom - (value / yMax) * PLOT_HEIGHT;

  return (
    <div ref={containerRef} onPointerLeave={hide} className="chart-body">
      <svg
        role="img"
        aria-label={ariaLabel}
        width="100%"
        viewBox={`0 0 ${width} ${plotBottom + BOTTOM_ROOM}`}
        style={{ display: "block" }}
        preserveAspectRatio="xMidYMid meet"
      >
        <defs>
          <clipPath id={`${gradientId}-clip`}>
            <rect x={0} y={0} width={width} height={plotBottom + BOTTOM_ROOM} />
          </clipPath>
        </defs>
        {/* hairline y-grid + tick labels */}
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
              {t.toLocaleString()}
            </text>
          </g>
        ))}
        {bins.map((bin, i) => {
          const slot =
            (plotWidth - 24) / bins.length;
          const cx = LEFT_AXIS_ROOM + 12 + i * slot + slot / 2;
          const barWidth = Math.min(BAR_MAX_WIDTH, slot - 10);
          const barHeight = (bin.value / yMax) * PLOT_HEIGHT;
          const h = Math.max(barHeight, bin.value > 0 ? 2 : 0);
          const top = plotBottom - h;
          return (
            <g key={bin.label}>
              <path
                d={columnPath(cx - barWidth / 2, top, barWidth, h, 4)}
                fill={bin.color ?? "var(--series-current)"}
                tabIndex={0}
                role="listitem"
                aria-label={`${bin.label}: ${formatValue(bin.value)}`}
                onPointerMove={(e) =>
                  show(
                    e,
                    <>
                      <TipTitle>{tooltipTitle ? tooltipTitle(bin) : bin.label}</TipTitle>
                      <TipRow label={bin.label} value={formatValue(bin.value)} swatch={bin.color} />
                      {bin.tooltip}
                    </>,
                  )
                }
                onFocus={(e) => {
                  const rect = (e.currentTarget as SVGGraphicsElement).getBoundingClientRect();
                  show(
                    { clientX: rect.left + rect.width / 2, clientY: rect.top },
                    <>
                      <TipTitle>{tooltipTitle ? tooltipTitle(bin) : bin.label}</TipTitle>
                      <TipRow label={bin.label} value={formatValue(bin.value)} swatch={bin.color} />
                      {bin.tooltip}
                    </>,
                  );
                }}
                onBlur={hide}
              />
              <text
                className="axis-text"
                x={cx}
                y={plotBottom + 16}
                textAnchor="middle"
              >
                {bin.label}
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
          y1={plotBottom}
          y2={plotBottom}
        />
      </svg>
      <ChartTip tip={tip} />
      {tableCaption && (
        <ChartTable
          caption={tableCaption}
          head={["Bucket", "Count"]}
          rows={bins.map((b) => [b.label, formatValue(b.value)])}
        />
      )}
    </div>
  );
}