// Tooltip plumbing shared by every chart. The tip is absolutely
// positioned inside the chart card and follows the pointer with a
// small offset; it flips horizontally near the right edge.

import { useCallback, useRef, useState } from "react";
import type { ReactNode } from "react";

export interface TipState {
  x: number;
  y: number;
  content: ReactNode;
}

export function useChartTooltip() {
  const containerRef = useRef<HTMLDivElement>(null);
  const [tip, setTip] = useState<TipState | null>(null);

  const show = useCallback((event: { clientX: number; clientY: number }, content: ReactNode) => {
    const host = containerRef.current;
    if (!host) return;
    const rect = host.getBoundingClientRect();
    const x = event.clientX - rect.left + 14;
    const y = event.clientY - rect.top + 14;
    const width = rect.width;
    setTip({
      x: x > width - 180 ? x - 190 : x,
      y: Math.max(4, y),
      content,
    });
  }, []);

  const hide = useCallback(() => setTip(null), []);

  return { containerRef, tip, show, hide };
}

export function ChartTip({ tip }: { tip: TipState | null }) {
  if (!tip) return null;
  return (
    <div className="viz-tip" style={{ left: tip.x, top: tip.y }} role="presentation">
      {tip.content}
    </div>
  );
}

export function TipTitle({ children }: { children: ReactNode }) {
  return <div className="tip-title">{children}</div>;
}

export function TipRow({
  label,
  value,
  swatch,
}: {
  label: ReactNode;
  value: ReactNode;
  swatch?: string;
}) {
  return (
    <div className="tip-row">
      <span className="tip-key" style={swatch ? ({ ["--key" as string]: swatch } as React.CSSProperties) : undefined}>
        {label}
      </span>
      <strong>{value}</strong>
    </div>
  );
}

/** Accessible table twin rendered inside a collapsible details block. */
export function ChartTable({
  caption,
  head,
  rows,
}: {
  caption: string;
  head: string[];
  rows: (ReactNode[])[];
}) {
  return (
    <details className="mt-2" style={{ padding: "0 8px 8px" }}>
      <summary
        style={{
          cursor: "pointer",
          fontSize: 12,
          color: "var(--text-3)",
          userSelect: "none",
        }}
      >
        Table view
      </summary>
      <div className="table-wrap" style={{ marginTop: 8 }}>
        <table className="data-table" style={{ fontSize: 12 }}>
          <caption className="muted" style={{ textAlign: "left", padding: "4px 2px 8px", fontSize: 12 }}>
            {caption}
          </caption>
          <thead>
            <tr>
              {head.map((h, i) => (
                <th key={i} style={i > 0 ? { textAlign: "right" } : undefined}>
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, i) => (
              <tr key={i}>
                {row.map((cell, j) => (
                  <td key={j} className={j > 0 ? "num" : undefined}>
                    {cell}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  );
}