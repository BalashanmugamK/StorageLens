// Settings — environment configuration, data provenance and project
// facts. No AWS credentials or URLs are hardcoded or displayed here;
// the API base URL comes from VITE_API_BASE_URL at build time and is
// shown only as configured / not configured.

import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, isConfigured } from "../services/api";
import { loadExperiments } from "../data/experiments";
import type { ExperimentBundle } from "../types";
import { LiveBadge, ExperimentBadge, StatTile } from "../components/ui/atoms";
import { formatCount } from "../utils/format";
import { PRICING_REGION } from "../utils/pricing";

export default function Settings() {
  const configured = isConfigured();
  const [bundle, setBundle] = useState<ExperimentBundle | null>(null);
  const [checking, setChecking] = useState(!configured);
  const [reachable, setReachable] = useState<boolean | null>(null);
  const [docCount, setDocCount] = useState<number | null>(null);

  useEffect(() => {
    loadExperiments().then(setBundle);
  }, []);

  useEffect(() => {
    if (!configured) return;
    setChecking(true);
    api
      .listDocuments()
      .then((result) => {
        setReachable(true);
        setDocCount(result.documents.length);
      })
      .catch(() => setReachable(false))
      .finally(() => setChecking(false));
  }, [configured]);

  const status = !configured
    ? { label: "Not configured", tone: "var(--warning)", detail: "VITE_API_BASE_URL is not set for this build." }
    : checking
      ? { label: "Checking…", tone: "var(--text-2)", detail: "Querying GET /documents." }
      : reachable
        ? { label: "Connected", tone: "var(--accent)", detail: `GET /documents returned ${docCount !== null ? formatCount(docCount) : "—"} documents.` }
        : { label: "Unreachable", tone: "var(--danger)", detail: "The API endpoint did not respond. Check the gateway deployment and CORS settings." };

  return (
    <div className="page-in">
      <header style={{ marginBottom: 18 }}>
        <h1 style={{ fontSize: 21, letterSpacing: "-0.02em" }}>Settings</h1>
        <p style={{ color: "var(--text-2)", marginTop: 4, fontSize: 13.5 }}>
          Environment configuration, data provenance and project facts. First
          time here? Read the operator tour on{" "}
          <Link to="/guide">How to use StorageLens</Link>.
        </p>
      </header>

      {/* API connection */}
      <section className="panel">
        <div className="panel-head">
          <div className="panel-title">API connection</div>
          <LiveBadge />
        </div>
        <div style={{ padding: "14px 22px 20px" }}>
          <div
            className="row"
            style={{
              border: "1px solid var(--border)",
              borderRadius: "var(--radius)",
              background: "var(--panel-2)",
              padding: "14px 16px",
              gap: 10,
            }}
          >
            <span
              aria-hidden="true"
              style={{
                width: 9,
                height: 9,
                borderRadius: 999,
                background: status.tone,
                boxShadow: `0 0 10px ${status.tone}`,
                flex: "none",
              }}
            />
            <div style={{ flex: 1 }}>
              <div style={{ fontWeight: 600, fontSize: 13.5 }}>{status.label}</div>
              <div style={{ color: "var(--text-2)", fontSize: 12.5, marginTop: 2 }}>{status.detail}</div>
            </div>
          </div>

          <div
            className="mt-2"
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))",
              gap: 10,
            }}
          >
            <div
              style={{
                border: "1px solid var(--border)",
                borderRadius: "var(--radius-sm)",
                padding: "12px 14px",
                background: "var(--panel-2)",
              }}
            >
              <div className="stat-label">Base URL configuration</div>
              <p style={{ fontSize: 12.5, color: "var(--text-2)", lineHeight: 1.6, marginTop: 4 }}>
                StorageLens reads a single variable at build time:{" "}
                <span className="mono" style={{ color: "var(--text)" }}>VITE_API_BASE_URL</span>.
                Copy <span className="mono">frontend/.env.example</span> to{" "}
                <span className="mono">frontend/.env</span>, set the value to
                your API Gateway base URL, and rebuild. <span className="mono">.env</span> is
                git-ignored and never committed. The credentials that sign
                request URLs live only in the backend — the browser never sees
                them.
              </p>
            </div>
            <div
              style={{
                border: "1px solid var(--border)",
                borderRadius: "var(--radius-sm)",
                padding: "12px 14px",
                background: "var(--panel-2)",
              }}
            >
              <div className="stat-label">Upload path</div>
              <p style={{ fontSize: 12.5, color: "var(--text-2)", lineHeight: 1.6, marginTop: 4 }}>
                Files go to S3 via a presigned PUT URL returned by{" "}
                <span className="mono">POST /documents</span> — the bytes are
                uploaded directly to S3, never proxied through API Gateway.
                Downloads use a presigned GET that also records a DOWNLOAD
                access event.
              </p>
            </div>
          </div>
        </div>
      </section>

      {/* Data provenance */}
      <section className="panel mt-3">
        <div className="panel-head">
          <div className="panel-title">Data provenance</div>
          <ExperimentBadge note="Experiment data" />
        </div>
        <div style={{ padding: "14px 22px 20px" }}>
          <p style={{ fontSize: 12.5, color: "var(--text-2)", lineHeight: 1.65 }}>
            The dashboard, optimization, policies and experiments pages render
            offline experiment artifacts, mirrored into the bundle at build
            time by <span className="mono">scripts/build_frontend_experiment_data.py</span>.
            Nothing on those pages is a live API metric, and the live documents
            table never inherits experiment numbers.
          </p>
          <div
            className="mt-2"
            style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))", gap: 10 }}
          >
            <div
              style={{
                border: "1px solid var(--border)",
                borderRadius: "var(--radius-sm)",
                padding: "12px 14px",
                background: "var(--panel-2)",
              }}
            >
              <div className="stat-label">Derived from</div>
              {bundle ? (
                <ul
                  style={{
                    margin: "6px 0 0",
                    padding: 0,
                    listStyle: "none",
                    fontSize: 12,
                    color: "var(--text-2)",
                    display: "flex",
                    flexDirection: "column",
                    gap: 4,
                  }}
                >
                  {bundle.meta.derived_from.map((path) => (
                    <li key={path} className="mono" style={{ wordBreak: "break-all" }}>
                      {path}
                    </li>
                  ))}
                </ul>
              ) : (
                <div className="skeleton" style={{ height: 60, marginTop: 6, borderRadius: 6 }} />
              )}
            </div>
            <div
              style={{
                border: "1px solid var(--border)",
                borderRadius: "var(--radius-sm)",
                padding: "12px 14px",
                background: "var(--panel-2)",
              }}
            >
              <div className="stat-label">Pricing snapshot</div>
              <p style={{ fontSize: 12.5, color: "var(--text-2)", lineHeight: 1.6, marginTop: 4 }}>
                All modeled costs come from{" "}
                <span className="mono">optimization/pricing.json</span>, a
                committed snapshot for{" "}
                <strong style={{ color: "var(--text)" }}>{PRICING_REGION}</strong>{" "}
                validated against the AWS Price List API. Two lines (Deep
                Archive storage and restore requests) are documented as absent
                from the Price List API and keep snapshot values. The engine
                warns when a snapshot is older than 90 days — pricing is never
                queried live at optimization runtime.
              </p>
            </div>
          </div>
        </div>
      </section>

      {/* Project facts */}
      <section className="panel mt-3">
        <div className="panel-head">
          <div className="panel-title">Project facts</div>
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(170px, 1fr))", gap: 10, padding: "14px 22px 20px" }}>
          <StatTile label="Product" value="StorageLens" footer={<span className="muted" style={{ fontSize: 11 }}>Cost intelligence for cloud storage</span>} />
          <StatTile label="Cloud" value="AWS" footer={<span className="muted" style={{ fontSize: 11 }}>Region {PRICING_REGION}</span>} />
          <StatTile label="Backend" value="AWS SAM" footer={<span className="muted" style={{ fontSize: 11 }}>Lambda · DynamoDB · S3</span>} />
          <StatTile label="This frontend" value="React + Vite" footer={<span className="muted" style={{ fontSize: 11 }}>TypeScript · hand-rolled SVG charts</span>} />
        </div>
        <p className="panel-note" style={{ padding: "0 22px 18px", lineHeight: 1.6 }}>
          StorageLens is an independent cost-analytics project. It is not an
          AWS product, and it is not affiliated with or endorsed by Amazon Web
          Services.
        </p>
      </section>
    </div>
  );
}