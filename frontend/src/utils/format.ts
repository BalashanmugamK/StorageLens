// Formatting helpers. Money figures are projected annual costs derived
// from the optimizer's pricing snapshot — displayed without currency
// conversion and never inflated beyond what the engine returned.

import { TIER_LABELS, TIER_ORDER } from "../types";
import { PRICING_CURRENCY } from "./pricing";

export function formatMoney(value: number, opts?: { precision?: number }): string {
  const precision = opts?.precision;
  if (value === 0) return "$0";
  if (precision !== undefined) return `$${value.toFixed(precision)}`;
  if (Math.abs(value) < 0.01) {
    const fixed4 = value.toFixed(4);
    // A nonzero sub-microtenth amount keeps real digits rather than
    // displaying as a fake-looking $0.0000.
    if (Number(fixed4) !== 0) return `$${fixed4}`;
    return `$${value.toFixed(6)}`;
  }
  if (Math.abs(value) < 1) return `$${value.toFixed(2)}`;
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: PRICING_CURRENCY,
    maximumFractionDigits: Math.abs(value) < 100 ? 2 : 0,
  }).format(value);
}

export function formatMoneyCompact(value: number): string {
  if (Math.abs(value) >= 1000)
    return new Intl.NumberFormat("en-US", {
      style: "currency",
      currency: PRICING_CURRENCY,
      maximumFractionDigits: 0,
    }).format(value);
  return formatMoney(value);
}

export function formatCount(value: number): string {
  return new Intl.NumberFormat("en-US").format(value);
}

export function formatPercent(value: number, precision = 1): string {
  return `${value.toFixed(precision)}%`;
}

export function formatBytes(bytes: number): string {
  if (bytes === 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const exp = Math.min(
    Math.floor(Math.log(bytes) / Math.log(1024)),
    units.length - 1,
  );
  const scaled = bytes / 1024 ** exp;
  return `${scaled >= 100 ? scaled.toFixed(0) : scaled.toFixed(1)} ${units[exp]}`;
}

export function formatGb(gb: number): string {
  if (gb >= 1000) return `${(gb / 1024).toFixed(1)} TB`;
  return `${gb.toFixed(1)} GB`;
}

export function tierLabel(storageClass: string | null | undefined): string {
  if (!storageClass) return "—";
  return TIER_LABELS[storageClass] ?? storageClass;
}

export function tierClassDot(storageClass: string): string {
  const idx = TIER_ORDER.indexOf(storageClass as (typeof TIER_ORDER)[number]);
  return idx >= 0 ? `var(--tier-${idx + 1})` : "var(--series-current)";
}

export function formatDate(timestamp: string | null | undefined): string {
  if (!timestamp) return "—";
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleDateString("en-US", {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

export function formatDateTime(timestamp: string | null | undefined): string {
  if (!timestamp) return "—";
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString("en-US", {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

export function relativeTime(timestamp: string | null | undefined): string {
  if (!timestamp) return "never";
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) return "never";
  const seconds = Math.round((Date.now() - date.getTime()) / 1000);
  if (seconds < 5) return "just now";
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  const days = Math.round(hours / 24);
  if (days < 30) return `${Math.max(days, 1)} d ago`;
  return formatDate(timestamp);
}

export function daysSinceLastAccess(
  value: number | null | undefined,
): string {
  if (value === null || value === undefined) return "never";
  if (value < 1) return `${Math.round(value * 24)} h ago`;
  if (value < 30) return `${value.toFixed(value < 3 ? 1 : 0)} d ago`;
  if (value < 365) return `${Math.round(value / 30)} mo ago`;
  return `${(value / 365).toFixed(1)} yr ago`;
}

export function fileExtension(fileName: string): string {
  const match = /\.([a-z0-9]+)$/i.exec(fileName);
  return match ? match[1].toUpperCase() : "FILE";
}

export function formatSeconds(seconds: number): string {
  if (seconds >= 60) return `${(seconds / 60).toFixed(1)} min`;
  return `${seconds.toFixed(2)} s`;
}

export function shortCourt(court: string | null): string {
  if (!court) return "—";
  const match = /United States (?:District Court|Court of Appeals) ([^(]+)/i.exec(
    court,
  );
  if (match) return match[1].trim();
  return court;
}