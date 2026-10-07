// Application shell: compact sidebar, sticky top bar, responsive
// bottom navigation on small screens.

import { NavLink } from "react-router-dom";
import type { ReactNode } from "react";
import {
  BellRing,
  BookOpen,
  ClipboardCheck,
  Compass,
  FileText,
  FlaskConical,
  Gauge,
  Moon,
  Scale,
  Settings,
  Sun,
  Zap,
} from "lucide-react";
import { relativeTime } from "../../utils/format";
import { PRICING_REGION } from "../../utils/pricing";
import { AuthChip } from "./AuthChip";
import { useLiveSystem } from "../../hooks/useLiveSystem";
import { useTheme } from "../../hooks/useTheme";

/* Storage layers, one already optimized — an abstract mark for
   layered cloud storage with an optimization accent. */
function BrandMark({ size = 26 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 26 26"
      fill="none"
      aria-hidden="true"
      className="brand-mark"
    >
      <rect x="1.5" y="2" width="23" height="5.4" rx="2.4" fill="#a9cdf4" />
      <rect x="1.5" y="10.3" width="23" height="5.4" rx="2.4" fill="#5a88dc" />
      <rect x="1.5" y="18.6" width="23" height="5.4" rx="2.4" fill="#2a4c93" />
      <rect
        x="14.5"
        y="18.6"
        width="10"
        height="5.4"
        rx="2.4"
        fill="#3ddc97"
      />
      <path
        d="M15.8 21.3h3.2M17.4 19.7l1.8 1.6-1.8 1.6"
        stroke="#070a10"
        strokeWidth="1.1"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function NavItem({
  to,
  icon,
  label,
  sub,
}: {
  to: string;
  icon: ReactNode;
  label: string;
  sub?: string;
}) {
  return (
    <NavLink
      to={to}
      className={({ isActive }) => `nav-item${isActive ? " active" : ""}`}
      end={to === "/"}
      title={sub}
    >
      <span className="nav-ico">{icon}</span>
      <span className="mobile-hidden">{label}</span>
    </NavLink>
  );
}

function Sidebar() {
  return (
    <nav className="sidebar" aria-label="Primary">
      <div className="brand">
        <BrandMark />
        <div>
          <div className="brand-name">StorageLens</div>
          <div className="brand-sub mobile-hidden">Cost intelligence</div>
        </div>
      </div>
      <div className="nav">
        <div className="nav-label mobile-hidden">Operate</div>
        <NavItem to="/" icon={<Gauge size={15.5} strokeWidth={1.8} />} label="Dashboard" />
        <NavItem to="/documents" icon={<FileText size={15.5} strokeWidth={1.8} />} label="Documents" />
        <NavItem to="/onboarding" icon={<Compass size={15.5} strokeWidth={1.8} />} label="Onboarding" sub="Connect an existing bucket: check, import, aggregate" />
        <NavItem to="/approvals" icon={<ClipboardCheck size={15.5} strokeWidth={1.8} />} label="Approvals" sub="Approval queue, execution and the savings ledger" />
        <NavItem to="/oncall" icon={<BellRing size={15.5} strokeWidth={1.8} />} label="On-Call" sub="PagerDuty shift schedule, sync and the paging audit" />
        <div className="nav-label mobile-hidden">Engine</div>
        <NavItem to="/optimization" icon={<Zap size={15.5} strokeWidth={1.8} />} label="Optimization" />
        <NavItem to="/policies" icon={<Scale size={15.5} strokeWidth={1.8} />} label="Policies" />
        <div className="nav-label mobile-hidden">Research</div>
        <NavItem to="/experiments" icon={<FlaskConical size={15.5} strokeWidth={1.8} />} label="Experiments" />
        <NavItem to="/guide" icon={<BookOpen size={15.5} strokeWidth={1.8} />} label="Guide" sub="How to use StorageLens" />
      </div>
      <div className="sidebar-foot mobile-hidden">
        <NavItem
          to="/settings"
          icon={<Settings size={15.5} strokeWidth={1.8} />}
          label="Settings"
        />
      </div>
    </nav>
  );
}

function MobileNav() {
  const items = [
    { to: "/", label: "Dashboard", icon: <Gauge size={18} strokeWidth={1.7} /> },
    { to: "/documents", label: "Docs", icon: <FileText size={18} strokeWidth={1.7} /> },
    { to: "/approvals", label: "Approve", icon: <ClipboardCheck size={18} strokeWidth={1.7} /> },
    { to: "/optimization", label: "Engine", icon: <Zap size={18} strokeWidth={1.7} /> },
    { to: "/experiments", label: "Tests", icon: <FlaskConical size={18} strokeWidth={1.7} /> },
    { to: "/settings", label: "More", icon: <Settings size={18} strokeWidth={1.7} /> },
  ];
  return (
    <nav className="mobile-nav" aria-label="Primary (mobile)">
      {items.map((item) => (
        <NavLink
          key={item.to}
          to={item.to}
          end={item.to === "/"}
          className={({ isActive }) => `mobile-nav-item${isActive ? " active" : ""}`}
        >
          {item.icon}
          <span>{item.label}</span>
        </NavLink>
      ))}
    </nav>
  );
}

function ThemeToggle() {
  const { theme, toggle } = useTheme();
  return (
    <button
      type="button"
      className="theme-toggle"
      aria-label={theme === "dark" ? "Switch to light theme" : "Switch to dark theme"}
      title={theme === "dark" ? "Switch to light theme" : "Switch to dark theme"}
      onClick={toggle}
    >
      {theme === "dark" ? <Sun size={14.5} strokeWidth={1.8} /> : <Moon size={14.5} strokeWidth={1.8} />}
    </button>
  );
}

function TopBar({
  status,
  lastAggregation,
}: {
  status:
    | "unconfigured"
    | "connecting"
    | "online"
    | "signed-out"
    | "offline";
  lastAggregation: { timestamp: string } | null;
}) {
  const statusLabel: Record<typeof status, string> = {
    unconfigured: "API not configured",
    connecting: "Connecting…",
    online: "Connected",
    "signed-out": "Signed out",
    offline: "API unreachable",
  };

  return (
    <header className="topbar">
      <div className="topbar-crumb">
        <strong>StorageLens</strong>
        <span className="muted">/</span>
        <span>Console</span>
      </div>
      <div className="topbar-spacer" />
      <span className="meta-chip hide-mobile" title="Pricing snapshot region (optimization/pricing.json)">
        <small>AWS</small> {PRICING_REGION}
      </span>
      <AuthChip />
      <span
        className="meta-chip"
        title={
          status === "online"
            ? "Backend API responded to the last health check"
            : status === "signed-out"
              ? "The API requires sign-in; every route sits behind the JWT authorizer"
              : status === "offline"
                ? "The last request to the backend failed"
                : undefined
        }
      >
        <span className="muted">API</span>
        <span
          className={`status-dot ${
            status === "online"
              ? "ok"
              : status === "offline" || status === "signed-out"
                ? "warn"
                : "pending pulse"
          }`}
          aria-hidden="true"
        />
        <span>{statusLabel[status]}</span>
      </span>
      <ThemeToggle />
      {lastAggregation && (
        <span className="meta-chip hide-mobile" title="Last aggregation run from this browser">
          <small>Last aggregation</small>
          <span className="tnum">{relativeTime(lastAggregation.timestamp)}</span>
        </span>
      )}
    </header>
  );
}

export function AppShell({ children }: { children: ReactNode }) {
  const { status, lastAggregation } = useLiveSystem();

  return (
    <div className="shell">
      <Sidebar />
      <div className="main-col">
        <TopBar status={status} lastAggregation={lastAggregation} />
        <main className="content">{children}</main>
      </div>
      <MobileNav />
    </div>
  );
}