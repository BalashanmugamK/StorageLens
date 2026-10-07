// Documents — the live system's inventory. Every row comes from the
// documents API; the recommendation / savings columns are experiment
// artifacts joined by api_document_id and labeled as such in the
// column header (and once per page in the footnote).

import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Search, Upload } from "lucide-react";
import { useLiveSystem } from "../hooks/useLiveSystem";
import {
  toJoinedRecommendation,
  useExperimentJoin,
} from "../hooks/useExperimentJoin";
import { UploadDialog } from "../components/documents/UploadDialog";
import { ImportBucketPanel } from "../components/documents/ImportBucketPanel";
import { DownloadButton } from "../components/documents/DownloadButton";
import {
  EmptyState,
  ErrorState,
  LiveBadge,
  Pagination,
  SignedOutNote,
  TierDot,
} from "../components/ui/atoms";
import {
  fileExtension,
  formatBytes,
  formatCount,
  formatMoney,
  tierLabel,
} from "../utils/format";
import { TIER_ORDER } from "../types";
import type { ApiDocument } from "../types";

const PAGE_SIZE = 25;

type SortKey = "name" | "size" | "savings" | "access" | "recency";
type SourceFilter = "all" | "synthetic" | "real";

/** Synthetic seeds carry a "synthetic-…" document id; every live real
 * document (GovInfo USCOURTS opinions, CFR / U.S. Code PDFs) gets a
 * UUID id from the API. */
function isSyntheticDoc(doc: ApiDocument): boolean {
  return doc.document_id.startsWith("synthetic");
}

const TIER_FILTERS = [
  { value: "all", label: "All tiers" },
  ...TIER_ORDER.map((tier) => ({ value: tier, label: tierLabel(tier) })),
];

const STATE_FILTERS = [
  { value: "all", label: "Any state" },
  { value: "ACTIVE", label: "Active" },
  { value: "CLOSED", label: "Closed" },
  { value: "ARCHIVED", label: "Archived" },
  { value: "None", label: "No state (—)" },
];

// "None" matches documents with no document_state on record (the "—"
// rows); see the state legend under the filter bar for what each state
// means for the optimizer.
const SOURCE_FILTERS = [
  { value: "all", label: "All sources" },
  { value: "synthetic", label: "Synthetic" },
  { value: "real", label: "Real (GovInfo)" },
] as const satisfies { value: SourceFilter; label: string }[];

export default function Documents() {
  const { status, documents, refreshDocuments } = useLiveSystem();
  const experimentJoin = useExperimentJoin();
  const [searchParams, setSearchParams] = useSearchParams();

  const [query, setQuery] = useState("");
  const [tierFilter, setTierFilter] = useState(
    searchParams.get("tier") ?? "all",
  );
  const [stateFilter, setStateFilter] = useState("all");
  const [sourceFilter, setSourceFilter] = useState<SourceFilter>("all");
  const [sortKey, setSortKey] = useState<SortKey>("size");
  const [sortDesc, setSortDesc] = useState(true);
  const [page, setPage] = useState(1);
  const [uploadOpen, setUploadOpen] = useState(false);

  // Keep the tier filter in the URL so dashboard opportunity cards
  // deep-link into a pre-filtered view.
  useEffect(() => {
    const param = searchParams.get("tier");
    if (param && param !== tierFilter) setTierFilter(param);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams]);

  useEffect(() => {
    setPage(1);
  }, [query, tierFilter, stateFilter, sourceFilter, sortKey, sortDesc]);

  // Source counts feed the filter's option labels (e.g. "Synthetic (500)")
  // so the split of the live inventory is visible before filtering.
  const sourceCount = useMemo(() => {
    let synthetic = 0;
    for (const doc of documents) if (isSyntheticDoc(doc)) synthetic += 1;
    return { synthetic, real: documents.length - synthetic };
  }, [documents]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    const rows = documents.filter((doc) => {
      if (q && !doc.file_name.toLowerCase().includes(q)) return false;
      if (sourceFilter === "synthetic" && !isSyntheticDoc(doc)) return false;
      if (sourceFilter === "real" && isSyntheticDoc(doc)) return false;
      if (stateFilter !== "all") {
        const state = doc.document_state || "None";
        if (state !== stateFilter) return false;
      }
      if (tierFilter !== "all") {
        const joined = experimentJoin ? experimentJoin[doc.document_id] : undefined;
        const recommended = joined
          ? toJoinedRecommendation(joined).recommendedTier
          : undefined;
        const matches =
          doc.current_storage_class === tierFilter ||
          recommended === tierFilter;
        if (!matches) return false;
      }
      return true;
    });

    const savingsOf = (id: string) => {
      const joined = experimentJoin?.[id];
      return joined ? toJoinedRecommendation(joined).savings : -Infinity;
    };
    const accessOf = (id: string) => {
      const joined = experimentJoin?.[id];
      return joined ? toJoinedRecommendation(joined).accessCount : -Infinity;
    };

    rows.sort((a, b) => {
      let cmp = 0;
      switch (sortKey) {
        case "name":
          cmp = a.file_name.localeCompare(b.file_name);
          break;
        case "size":
          cmp = a.file_size - b.file_size;
          break;
        case "savings":
          cmp = savingsOf(a.document_id) - savingsOf(b.document_id);
          break;
        case "access":
          cmp = accessOf(a.document_id) - accessOf(b.document_id);
          break;
        case "recency":
          cmp = a.upload_timestamp.localeCompare(b.upload_timestamp);
          break;
      }
      return sortDesc ? -cmp : cmp;
    });

    return rows;
  }, [documents, query, tierFilter, stateFilter, sourceFilter, sortKey, sortDesc, experimentJoin]);

  const pageCount = Math.max(Math.ceil(filtered.length / PAGE_SIZE), 1);
  const pageRows = filtered.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);

  const toggleSort = (key: SortKey) => {
    if (key === sortKey) setSortDesc((d) => !d);
    else {
      setSortKey(key);
      setSortDesc(key !== "name");
    }
  };

  const sortIndicator = (key: SortKey) =>
    sortKey === key ? (sortDesc ? " ↓" : " ↑") : "";

  const recommendedCount = useMemo(() => {
    if (!experimentJoin) return 0;
    return documents.filter((d) => experimentJoin[d.document_id]).length;
  }, [documents, experimentJoin]);

  return (
    <div className="page-in">
      <header
        className="page-in"
        style={{
          display: "flex",
          alignItems: "flex-end",
          justifyContent: "space-between",
          gap: 18,
          flexWrap: "wrap",
          marginBottom: 18,
        }}
      >
        <div>
          <div className="row" style={{ gap: 10 }}>
            <h1 style={{ fontSize: 21, letterSpacing: "-0.02em" }}>Documents</h1>
            <LiveBadge />
          </div>
          <p style={{ color: "var(--text-2)", marginTop: 4, fontSize: 13.5 }}>
            {status === "online"
              ? `${formatCount(documents.length)} documents registered with the live API`
              : "Live inventory of the documents table"}
          </p>
        </div>
        <div className="row">
          <ImportBucketPanel
            disabled={status !== "online"}
            onImported={refreshDocuments}
          />
          <button
            type="button"
            className="btn"
            onClick={refreshDocuments}
            disabled={status === "connecting"}
          >
            Refresh
          </button>
          <button
            type="button"
            className="btn btn-primary"
            onClick={() => setUploadOpen(true)}
            disabled={status !== "online"}
            title={status === "online" ? undefined : "Upload needs a reachable backend"}
          >
            <Upload size={14} /> Upload
          </button>
        </div>
      </header>

      {/* Filter bar */}
      <div
        className="row"
        style={{
          flexWrap: "wrap",
          marginBottom: 14,
          gap: 8,
        }}
      >
        <div style={{ position: "relative", flex: "1 1 220px", maxWidth: 340 }}>
          <Search
            size={14}
            style={{
              position: "absolute",
              left: 10,
              top: "50%",
              transform: "translateY(-50%)",
              color: "var(--text-faint)",
              pointerEvents: "none",
            }}
          />
          <input
            className="input"
            placeholder="Search file names…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            style={{ width: "100%", paddingLeft: 30 }}
            aria-label="Search file names"
          />
        </div>
        <select
          className="select"
          value={tierFilter}
          onChange={(e) => {
            setTierFilter(e.target.value);
            const next = new URLSearchParams(searchParams);
            if (e.target.value === "all") next.delete("tier");
            else next.set("tier", e.target.value);
            setSearchParams(next, { replace: true });
          }}
          aria-label="Filter by recommended tier"
        >
          {TIER_FILTERS.map((f) => (
            <option key={f.value} value={f.value}>
              {f.label}
            </option>
          ))}
        </select>
        <select
          className="select"
          value={sourceFilter}
          onChange={(e) => setSourceFilter(e.target.value as SourceFilter)}
          aria-label="Filter by dataset source"
        >
          {SOURCE_FILTERS.map((f) => (
            <option key={f.value} value={f.value}>
              {f.value === "all"
                ? f.label
                : `${f.label} (${formatCount(
                    f.value === "synthetic"
                      ? sourceCount.synthetic
                      : sourceCount.real,
                  )})`}
            </option>
          ))}
        </select>
        <select
          className="select"
          value={stateFilter}
          onChange={(e) => setStateFilter(e.target.value)}
          aria-label="Filter by document state"
        >
          {STATE_FILTERS.map((f) => (
            <option key={f.value} value={f.value}>
              {f.label}
            </option>
          ))}
        </select>
        <span className="muted tnum" style={{ fontSize: 12.5, marginLeft: "auto" }}>
          {formatCount(filtered.length)}
          {filtered.length !== documents.length &&
            ` of ${formatCount(documents.length)}`}{" "}
          shown
        </span>
      </div>

      {/* State legend — the state is what the optimizer's tier
          eligibility constraint (optimization/constraints.py) is driven
          by, so spell out what each value lets the engine recommend. */}
      <p
        className="muted"
        style={{
          fontSize: 11.5,
          lineHeight: 1.6,
          margin: "-4px 0 14px",
          maxWidth: 980,
        }}
      >
        What the states mean: <strong>Active</strong> — case in progress; the
        optimizer may only recommend fast, retrieval-friendly tiers (S3
        Standard, Standard-IA, Glacier IR). <strong>Closed</strong> — case
        concluded; mid tiers allowed (Standard-IA, Glacier IR, Glacier FR).{" "}
        <strong>Archived</strong> — long-held record; archive tiers allowed
        (Glacier IR/FR, Deep Archive). <strong>— (no state)</strong> — nothing
        on record, so the optimizer may pick any of the five tiers. Synthetic
        vs real: this inventory mixes the synthetic benchmark documents with
        real GovInfo files loaded into the live bucket — use the Source
        filter above to separate them.
      </p>

      {status === "signed-out" && (
        <div className="panel">
          <SignedOutNote />
        </div>
      )}

      {status === "offline" && (
        <div className="panel">
          <ErrorState onRetry={refreshDocuments} />
        </div>
      )}

      {status === "unconfigured" && (
        <div className="panel">
          <EmptyState
            title="No API base URL configured"
            description="Point VITE_API_BASE_URL at your deployed API Gateway endpoint (see frontend/.env.example) to browse the live document inventory."
          />
        </div>
      )}

      {(status === "online" || status === "connecting") && (
        <div
          className="panel"
          style={{
            opacity: status === "connecting" ? 0.55 : 1,
            transition: "opacity .2s",
          }}
        >
          {status === "online" && documents.length === 0 ? (
            <EmptyState
              title="No live documents yet"
              description="The documents table is empty. Upload a document, or load the seeded dataset with scripts/load_real_data.py."
            />
          ) : status === "online" && filtered.length === 0 ? (
            <EmptyState
              title="No documents match these filters"
              description="Adjust the search or filters to revisit the rest of the inventory."
            />
          ) : (
            <>
              {/* Desktop table */}
              <div className="table-wrap">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th className="sortable" onClick={() => toggleSort("name")}>
                        Document{sortIndicator("name")}
                      </th>
                      <th className="sortable num" onClick={() => toggleSort("size")}>
                        Size{sortIndicator("size")}
                      </th>
                      <th>Storage tier</th>
                      <th title="Lifecycle state recorded on the document — State constrains which tiers the optimizer may recommend (Active → fast tiers, Closed → mid tiers, Archived → archive tiers, — → any tier)">
                        State
                      </th>
                      <th className="sortable num" onClick={() => toggleSort("access")} title="Access counts are experiment-modeled">
                        30-day accesses{sortIndicator("access")}
                      </th>
                      <th title="Experiment-modeled recommendations, joined by document id">
                        Recommendation ★
                      </th>
                      <th className="sortable num" onClick={() => toggleSort("savings")}>
                        Savings / yr{sortIndicator("savings")}
                      </th>
                      <th style={{ width: 110 }}>Actions</th>
                    </tr>
                  </thead>
                  <tbody>
                    {status === "connecting" ? (
                      <tr>
                        <td colSpan={8}>
                          <div className="skeleton skeleton-row" style={{ margin: 0 }} />
                        </td>
                      </tr>
                    ) : (
                      pageRows.map((doc) => {
                        const joined = experimentJoin?.[doc.document_id];
                        const rec = joined
                          ? toJoinedRecommendation(joined)
                          : null;
                        return (
                          <tr key={doc.document_id}>
                            <td>
                              <Link
                                to={`/documents/${doc.document_id}`}
                                className="doc-cell"
                                style={{ textDecoration: "none" }}
                              >
                                <span className="doc-ico" style={{ fontSize: 8.5, fontWeight: 700 }}>
                                  {fileExtension(doc.file_name)}
                                </span>
                                <span>
                                  <span className="doc-name" title={doc.file_name}>{doc.file_name}</span>
                                  <div className="doc-sub">
                                    {isSyntheticDoc(doc) ? (
                                      <span
                                        className="badge badge-experiment"
                                        style={{ fontSize: 9, padding: "1px 6px" }}
                                      >
                                        Synthetic
                                      </span>
                                    ) : (
                                      <span style={{ fontSize: 9.5, color: "var(--text-3)" }}>
                                        GovInfo · real
                                      </span>
                                    )}
                                    <span> · {doc.content_type}</span>
                                  </div>
                                </span>
                              </Link>
                            </td>
                            <td className="num tnum">{formatBytes(doc.file_size)}</td>
                            <td>
                              <span className="row tier-move" style={{ gap: 6 }}>
                                <TierDot tier={doc.current_storage_class} />
                                <span>{tierLabel(doc.current_storage_class)}</span>
                              </span>
                            </td>
                            <td>
                              {doc.document_state ? (
                                <span className="badge badge-neutral">
                                  {doc.document_state}
                                </span>
                              ) : (
                                <span className="muted">—</span>
                              )}
                            </td>
                            <td className="num tnum">
                              {rec ? rec.accessCount : "—"}
                            </td>
                            <td>
                              {rec ? (
                                <span className="row tier-move" style={{ gap: 6 }}>
                                  <span className="tier-pill">{tierLabel(doc.current_storage_class)}</span>
                                  <span className="muted">→</span>
                                  <span
                                    className="tier-pill"
                                    style={{
                                      color:
                                        rec.recommendedTier === doc.current_storage_class
                                          ? "var(--text-2)"
                                          : "var(--accent-strong)",
                                    }}
                                  >
                                    {tierLabel(rec.recommendedTier)}
                                  </span>
                                </span>
                              ) : (
                                <span className="muted">—</span>
                              )}
                            </td>
                            <td className="num">
                              {rec ? (
                                <span
                                  className="tnum"
                                  style={{
                                    color:
                                      rec.savings > 0
                                        ? "var(--accent-strong)"
                                        : rec.savings < 0
                                          ? "var(--danger)"
                                          : "var(--text-3)",
                                  }}
                                >
                                  {rec.savings > 0
                                    ? `Save ${formatMoney(rec.savings)}`
                                    : rec.savings < 0
                                      ? `Costs ${formatMoney(-rec.savings)} more`
                                      : "—"}
                                </span>
                              ) : (
                                <span className="muted">—</span>
                              )}
                            </td>
                            <td>
                              <div className="row" style={{ gap: 4 }}>
                                <Link
                                  to={`/documents/${doc.document_id}`}
                                  className="btn btn-sm btn-ghost"
                                  style={{ textDecoration: "none" }}
                                  aria-label={`Open ${doc.file_name}`}
                                >
                                  View
                                </Link>
                                <DownloadButton documentId={doc.document_id} />
                              </div>
                            </td>
                          </tr>
                        );
                      })
                    )}
                  </tbody>
                </table>
              </div>

              {/* Mobile cards */}
              <div className="mobile-cards">
                {pageRows.map((doc) => {
                  const joined = experimentJoin?.[doc.document_id];
                  const rec = joined ? toJoinedRecommendation(joined) : null;
                  return (
                    <Link
                      to={`/documents/${doc.document_id}`}
                      key={doc.document_id}
                      className="doc-card"
                      style={{ textDecoration: "none" }}
                    >
                      <div className="row" style={{ justifyContent: "space-between" }}>
                        <span className="row" style={{ gap: 9 }}>
                          <span className="doc-ico" style={{ fontSize: 8, fontWeight: 700 }}>
                            {fileExtension(doc.file_name)}
                          </span>
                          <span>
                            <span className="doc-name" title={doc.file_name}>{doc.file_name}</span>
                            <div className="doc-sub tnum">
                              {formatBytes(doc.file_size)} ·{" "}
                              {isSyntheticDoc(doc) ? "Synthetic" : "GovInfo · real"}
                            </div>
                          </span>
                        </span>
                        {rec && rec.savings > 0 && (
                          <span className="tnum" style={{ color: "var(--accent-strong)", fontSize: 12 }}>
                            Save {formatMoney(rec.savings, { precision: 3 })}/yr
                          </span>
                        )}
                      </div>
                      <div className="row mt-1" style={{ gap: 8, flexWrap: "wrap" }}>
                        <span className="tier-pill row" style={{ gap: 6 }}>
                          <TierDot tier={doc.current_storage_class} />
                          {tierLabel(doc.current_storage_class)}
                        </span>
                        {rec && rec.recommendedTier !== doc.current_storage_class && (
                          <span
                            className="tier-pill row"
                            style={{ gap: 6, color: "var(--accent-strong)" }}
                          >
                            → {tierLabel(rec.recommendedTier)}
                          </span>
                        )}
                        {doc.document_state && (
                          <span className="badge badge-neutral">{doc.document_state}</span>
                        )}
                        {rec && (
                          <span className="muted tnum" style={{ fontSize: 11 }}>
                            {rec.accessCount} accesses (30 d)
                          </span>
                        )}
                      </div>
                    </Link>
                  );
                })}
              </div>

              <Pagination
                page={page}
                pageCount={pageCount}
                onPage={setPage}
                totalItems={filtered.length}
              />

              <div
                className="row"
                style={{
                  padding: "10px 16px 14px",
                  borderTop: "1px solid var(--border)",
                  gap: 8,
                  fontSize: 11.5,
                  color: "var(--text-3)",
                }}
              >
                <span className="badge badge-experiment">★ Experiment</span>
                <span>
                  Recommendation, access and savings columns are modeled by the
                  offline optimizer over its pricing snapshot —{" "}
                  {formatCount(recommendedCount)} of {formatCount(documents.length)}{" "}
                  live documents have a matching experiment result.
                </span>
              </div>
            </>
          )}
        </div>
      )}

      <UploadDialog
        open={uploadOpen}
        onClose={() => setUploadOpen(false)}
        onUploaded={() => refreshDocuments()}
      />
    </div>
  );
}