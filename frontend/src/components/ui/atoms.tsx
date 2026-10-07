// Small shared UI atoms: badges, stat tiles, state blocks, pagination.

import type { ReactNode } from "react";
import { AlertTriangle, Inbox, RefreshCw, ServerOff } from "lucide-react";
import { TIER_ORDER } from "../../types";

/* ---------- Data-provenance badges ---------- */

export function ExperimentBadge({ note }: { note?: string }) {
  return (
    <span
      className="badge badge-experiment"
      title={
        note ??
        "Value from an offline experiment artifact, not the live system"
      }
    >
      Experiment
    </span>
  );
}

export function LiveBadge() {
  return (
    <span
      className="badge badge-live"
      title="Fetched from the live backend API"
    >
      Live
    </span>
  );
}

/** Shown where live data cannot load because the API's JWT
    authorizer rejected the request. MUST NOT look like a network
    failure — the backend is reachable. */
export function SignedOutNote() {
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
        Sign in required — every API route carries the JWT authorizer, so
        sign in with the top-bar control to load live data.
      </span>
    </div>
  );
}

export function TierDot({ tier }: { tier: string }) {
  return (
    <span
      className="tier-dot"
      style={{
        background: `var(--tier-${tierIndex(tier) + 1})`,
      }}
      aria-hidden="true"
    />
  );
}

export function tierIndex(tier: string): number {
  const idx = TIER_ORDER.indexOf(
    tier as (typeof TIER_ORDER)[number],
  );
  return idx >= 0 ? idx : 0;
}

/* ---------- Stat tiles ---------- */

export function StatTile({
  label,
  value,
  delta,
  deltaTone,
  footer,
  hero,
  children,
}: {
  label: ReactNode;
  value: ReactNode;
  delta?: ReactNode;
  deltaTone?: "good" | "bad" | undefined;
  footer?: ReactNode;
  hero?: boolean;
  children?: ReactNode;
}) {
  return (
    <div className="stat">
      <div className="stat-label">{label}</div>
      <div className={`stat-value${hero ? " hero" : ""}`}>{value}</div>
      {delta !== undefined && (
        <div
          className={`stat-delta${deltaTone ? ` ${deltaTone}` : ""}`}
        >
          {delta}
        </div>
      )}
      {footer}
      {children}
    </div>
  );
}

/* ---------- Page section ---------- */

export function PageHeader({
  title,
  subtitle,
  children,
}: {
  title: string;
  subtitle?: string;
  children?: ReactNode;
}) {
  return (
    <header
      className="page-in"
      style={{
        display: "flex",
        alignItems: "flex-end",
        justifyContent: "space-between",
        gap: 18,
        flexWrap: "wrap",
        marginBottom: 22,
      }}
    >
      <div>
        <h1 style={{ fontSize: 21, letterSpacing: "-0.02em" }}>{title}</h1>
        {subtitle && (
          <p style={{ color: "var(--text-2)", marginTop: 4, fontSize: 13.5 }}>
            {subtitle}
          </p>
        )}
      </div>
      <div className="row">{children}</div>
    </header>
  );
}

/* ---------- Loading ---------- */

export function Spinner({ size = 16 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      aria-hidden="true"
      style={{ animation: "spin 0.9s linear infinite" }}
    >
      <style>{`@keyframes spin { to { transform: rotate(360deg) } }`}</style>
      <circle
        cx="12"
        cy="12"
        r="9"
        stroke="var(--border-strong)"
        strokeWidth="2.5"
      />
      <path
        d="M21 12a9 9 0 0 0-9-9"
        stroke="currentColor"
        strokeWidth="2.5"
        strokeLinecap="round"
      />
    </svg>
  );
}

export function TableSkeleton({ rows = 8 }: { rows?: number }) {
  return (
    <div style={{ padding: "16px 14px" }} aria-hidden="true">
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="skeleton skeleton-row" />
      ))}
    </div>
  );
}

/* ---------- Empty state ---------- */

export function EmptyState({
  title,
  description,
  actions,
  icon,
}: {
  title: string;
  description: string;
  actions?: ReactNode;
  icon?: ReactNode;
}) {
  return (
    <div className="state-block">
      <div className="state-icon">
        {icon ?? <Inbox size={20} strokeWidth={1.6} />}
      </div>
      <div className="state-title">{title}</div>
      <p className="state-desc">{description}</p>
      {actions && <div className="state-actions">{actions}</div>}
    </div>
  );
}

/* ---------- Error state ---------- */

export function ErrorState({
  title = "Unable to reach the optimization API",
  description,
  onRetry,
}: {
  title?: string;
  description?: string;
  onRetry?: () => void;
}) {
  return (
    <div className="state-block">
      <div className="state-icon">
        <ServerOff size={20} strokeWidth={1.6} />
      </div>
      <div className="state-title">{title}</div>
      <p className="state-desc">
        {description ??
          "Check the API URL, your network connection, and backend availability."}
      </p>
      {onRetry && (
        <div className="state-actions">
          <button type="button" className="btn" onClick={onRetry}>
            <RefreshCw size={14} /> Retry
          </button>
        </div>
      )}
    </div>
  );
}

/* ---------- Inline warning ---------- */

export function InlineWarning({
  title,
  children,
}: {
  title: ReactNode;
  children?: ReactNode;
}) {
  return (
    <div
      className="row"
      style={{
        border: "1px solid var(--warning-border)",
        background: "var(--warning-dim)",
        borderRadius: "var(--radius-sm)",
        padding: "10px 12px",
        fontSize: 12.5,
        color: "var(--warning)",
        alignItems: "flex-start",
      }}
    >
      <AlertTriangle size={15} style={{ flex: "none", marginTop: 1 }} />
      <div style={{ color: "var(--text-2)" }}>
        <strong style={{ color: "var(--warning)" }}>{title}</strong>
        {children && <div className="mt-1">{children}</div>}
      </div>
    </div>
  );
}

/* ---------- Pagination ---------- */

export function Pagination({
  page,
  pageCount,
  onPage,
  totalItems,
  itemLabel = "documents",
}: {
  page: number;
  pageCount: number;
  onPage: (page: number) => void;
  totalItems: number;
  itemLabel?: string;
}) {
  return (
    <div className="pager">
      <span className="tnum">
        {pageCount === 0 ? "No " + itemLabel : `Page ${page} of ${pageCount}`}
        {pageCount > 0 && (
          <span className="muted"> · {totalItems.toLocaleString()} {itemLabel}</span>
        )}
      </span>
      <div className="row" style={{ gap: 6 }}>
        <button
          type="button"
          className="btn btn-sm"
          disabled={page <= 1}
          onClick={() => onPage(page - 1)}
          aria-label="Previous page"
        >
          Prev
        </button>
        <button
          type="button"
          className="btn btn-sm"
          disabled={page >= pageCount}
          onClick={() => onPage(page + 1)}
          aria-label="Next page"
        >
          Next
        </button>
      </div>
    </div>
  );
}