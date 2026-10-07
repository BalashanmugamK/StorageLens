# PRODUCT AUDIT — Intelligent Cloud Storage Cost Optimizer

Date: 2026-10-07 · Reviewer: senior product architect / FinOps specialist / security reviewer / QA lead stance
Scope: full repo inspection + live competitor research. NO product files were modified (this report is the only file added).
Verification performed: 129/129 pytest pass · 50/50 vitest pass · engine parity copy diff-verified byte-identical · all headline numbers re-read from artifacts.

---

## 1. EXECUTIVE VERDICT

There is a real product kernel inside this repository, but the current deployment shape cannot become that product. The kernel is:

1. A per-object, billing-accurate cost model (minimum billable sizes, archived-object overhead, early-deletion fees, request economics) — verified against AWS's documented pricing mechanics.
2. A **human-gated execution loop with HEAD-verified transitions** — something almost none of the named competitors do at object level (they stop at bucket-level recommendations or tickets).
3. An unusual honesty discipline (LIVE vs EXPERIMENT provenance, labeled ledger basis) that survives vendor scrutiny.

What blocks product-hood is everything around the kernel: the system can only see documents that were uploaded through its own API (zero connectivity to an existing customer's buckets), the daily aggregation schedule silently no-ops (P0 bug, verified), "real corpus" experiments use synthetic backfilled access events, the architecture has a hard scale cliff near ~5,000 objects, and there is no way to connect a customer account safely (no CloudTrail/Inventory ingestion, no cross-account role story).

**Verdict: a strong university project with an unusually good trust/execution core. Not yet a product. There IS a defensible narrow product inside it — a policy-aware, explainable, human-gated S3 tier-optimizer for compliance-heavy single-org deployments — reachable with roughly 2–3 months of focused work. It cannot beat Intelligent-Tiering on automation, and should never try; it wins on governance, explanation, and verified outcomes.**

## 2. CURRENT PRODUCT DEFINITION

What actually exists (implemented, not aspirational):

| Layer | State |
|---|---|
| Cost engine (optimization/, 10 modules, pure Python) | Implemented, deterministic, test-pinned |
| Policy A (state-constrained argmin) / Policy B (cost-safe, current class joins candidates) | Implemented; B fixed on live path |
| Documents API (metadata, presigned PUT/GET 900 s, DOWNLOAD event logging, access-stats) | Implemented (own-API only) |
| Aggregation Lambda (30-day window, bounded thread pool, 6.8 s @ 500 docs vs 21.6 s seq) | Implemented; **daily schedule broken (P0)** |
| Decisions Lambda (propose/approve/reject/execute/verify/ledger, 6 invariants) | Implemented, test-pinned |
| StorageLens frontend (10 pages, LIVE/EXPERIMENT separation) | Implemented |
| Infra (SAM, Cognito PKCE, JWT authorizer, throttling 50 rps, alarms, log retention, deploy preflight) | Implemented, single-account/single-region |
| Pricing snapshot + refresh/diff tool (refuses bad writes, 90-day staleness warn) | Implemented (ap-south-1, checked 2026-09-10) |
| Data: synthetic legal 500; GovInfo diversified 500; us_courts 500 | Metadata real; access events synthetic |
| CI, multi-account, multi-region, invoice reconciliation, real-access ingestion | Missing |

What is simulated/experimental: every savings number in existence in this repo. The synthetic 96.2% headline is engine-vs-age-based-lifecycle under the engine's own model, over a generator-defined access mix (the optimizer effectively gets the answer key). The "real dataset" experiments use real documents with **synthetic, back-dated DOWNLOAD events reusing the same frequency distribution** (`scripts/load_real_data.py` header says so explicitly). No experiment anywhere observes real user access.

Manual steps today: sam deploy; deploy preflight; Cognito users created by hand; `npm install` + `.env` + dev server; data seeding scripts; run aggregation (manual button — schedule broken); propose per document (no bulk); approve; execute; execute-again (two-step); pricing refresh script; engine copy sync (test-pinned but manual).

## 3. ACTUAL ARCHITECTURE (traced end-to-end)

```
Real path (works):
SPA (Cognito PKCE, sessionStorage, refresh-on-401)
  → HTTP API (JWT authorizer; GET /config public; 50 rps/100 burst; wildcard CORS)
  → DocumentsFunction: POST /documents → presigned PUT (900 s) → DDB DocumentsTable
    GET /documents/{id}/download → presigned GET + AccessHistoryTable event (DOWNLOAD)
  → AggregatesFunction: POST /aggregates/run → Scan DocumentsTable
    → per-doc Query(AccessHistoryTable, 30-day window, bounded retries, ThreadPool)
    → compute 10-field aggregate → batch write DocumentAggregatesTable
    [SAM EventBridge schedule rate(1 day) → handler 404s it → P0]
  → DecisionsFunction: POST /decisions → aggregate item → from_aggregate_item
    → byte-pinned engine (optimize_document, Policy B) → DecisionsTable (proposed)
    → approve/reject (state machine + stamps) → execute (S3 CopyObject onto self)
    → HEAD verify (IT accepts STANDARD) → verified + realized_savings | failed
  → GET /ledger (sum realized_savings, basis string)

Broken/stale path (verified by grep):
  post-execute → DocumentsTable.current_storage_class is NEVER updated
  → next aggregate re-derives old class → engine re-proposes the same migration
  → the previously migrated object can be "verified" again → ledger double-counts
```

Frontend reads live API + a committed, provenance-pinned `experiments.json` bundle (meta carries generator, derived_from paths, region, checked date — enforced by vitest).

Engine copy: `backend/lambdas/decisions/engine/` diff-verified byte-identical to `optimization/` today.

## 4. CUSTOMER PERSONAS (brutally realistic)

1. **Small startup, 1–5 TB S3.** Problem: default-Standard drift. Connection: they will not run your SAM stack for this; they will flip on Intelligent-Tiering in the console in two minutes (free-ish, per-object fee only for objects ≥128 KB). Verdict: **no sale.** IT + lifecycle solves them. Your product has to be near-zero-touch; it is not.
2. **Mid-sized company, 50–500 TB.** Problem: real — Standard-heavy buckets, unknown cold fraction; IT feels risky ("what if retrieval patterns change"), lifecycle is age-blind. Connection: they could deploy the stack in-account after a security review. Today it cannot see their buckets (own-API only) — integration work = the product's whole missing half (Inventory/CloudTrail ingestion, Step Functions aggregation). If built: read-only discovery first, write role only during execution. Interest: **medium-high** if the approval loop + verified savings work as designed. This is the beachhead.
3. **Enterprise, multi-account, PB+.** Won't deploy a SAM stack per account informally; needs Org onboarding, external-ID cross-account roles, RBAC, CAGRA-style governance, SOC 2. Current architecture: **cannot serve.** FinOps platforms (Cloudability, CloudHealth, Vantage) already occupy the seat; enterprises buy breadth, not per-object depth. Realistic role later: a governed execution layer under a FinOps platform.
4. **Legal company.** The state-eligibility model (ACTIVE/CLOSED/ARCHIVED → allowed tiers) maps directly to legal matter lifecycle and compliance holds. Deep Archive latency surfaced informationally is right for this persona. Best-fit first customer. Needs: audit trail with actor identity (missing — no `approved_by`), retention policy integration, document-level conflict flags (present).
5. **Media company (large files).** Rarely accesses months-old 500 GB renders; per-object economics favor IR/IA correctly. Restores 5–12 h are a real workflow problem — the model surfaces but doesn't gate on them; acceptable if labeled. Multi-PB scale breaks the architecture; needs a bucket-segmented design. Later.
6. **Backup/archive workload.** Age-based lifecycle (their status quo, and your simulated baseline) is usually near-optimal because access is near-zero and predictable; your optimizer's edge is smallest here. Weak fit.
7. **Data lake / analytics.** 1M+ small objects, query-driven reads (not downloads). CloudTrail data events bill by volume (a busy lake logs millions of events — real cost, verified vs. published $0.10/100k rates), tiny-object minimums dominate, and IT monitoring fees exceed savings below ~250 KB average object size. Current engine assumes whole-object DOWNLOADs. Requires prefix/aggregate-level modeling. Later.

## 5. CUSTOMER WORKFLOW vs CURRENT CAPABILITY

| Stage | State | Notes |
|---|---|---|
| Customer signs up | RED | No sign-up; Cognito pool with open self-signup (no PreSignUp hook) |
| Connect AWS account | RED | Nothing connects; only self-created docs via own API |
| Select region/accounts/buckets | RED | Single-region single-stack |
| Read-only discovery | RED | No Inventory/CloudTrail/SCA ingestion |
| Analyze workload | YELLOW | Works — but only over self-created documents |
| Calculate current cost | YELLOW | Engine per-object; no invoice anchoring (CUR/CE absent) |
| Generate recommendations | GREEN* | Deterministic, explainable features + per-tier breakdowns (*at demo scale) |
| Explain why | GREEN | Tier-level breakdowns, features_as_of, eligibility + policy_conflict surfaced |
| Estimate savings | YELLOW | Model-honest, labeled as predictions; no confidence bands |
| Show risk / retrieval impact | YELLOW | `retrieval_time_hours` surfaced, not gated |
| Approval queue | GREEN | State machine + GSI queue + 409 guard (TOCTOU caveat) |
| Assume write role | GREEN | In-account only; the Lambda role *is* the write authority (no time-boxed role) |
| Execute migrations | GREEN | CopyObject onto self; POST-only; two-step UI confirm |
| Verify actual class | GREEN | HEAD verification, IT-specific acceptance map, failed path exists |
| Track realized savings | YELLOW | Ledger exists but basis = predicted; double-count bug present; no CUR reconciliation |
| Continue monitoring | RED | Schedule no-ops (P0); no notifications; no re-review cadence |

## 6. COMPETITIVE ANALYSIS (research: AWS docs / S3 pricing page / vendor docs, Oct 2026)

| Capability | S3 Intelligent-Tiering | S3 Lifecycle | S3 Storage Class Analysis | Storage Lens | Cloudability | CloudHealth | Vantage | **Ours** |
|---|---|---|---|---|---|---|---|---|
| Uses access patterns | Yes (per object, auto) | No (SCA-informed, manual) | Observes only | Dashboards (30-day activity, prefix advanced) | Yes (bucket/container level) | Yes (bucket level, weekly) | Yes (bucket level) | Yes (object level) |
| Auto-migrates | Yes (FA→IA→AIA; AA/DAA opt-in) | Yes (age rules) | No | No | No (recommendations/tickets) | No | No | **Yes (gated)** |
| Retrieval cost in model | n/a (none inside class) | n/a | n/a | n/a | Yes | No (explicitly excluded) | Not subtracted | Yes |
| Transition/early-deletion min durations | None in class | n/a | n/a | n/a | Yes | No | No | **Yes + prorated fee** |
| Min billable sizes / archive overhead | Fee waiver <128 KB | n/a | n/a | n/a | Likely | No | No | **Yes, per class** |
| Compliance/policy constraints | None (opt-in tiers only) | None | None | None | Tags/rules partial | Partial | No | **Yes (state eligibility)** |
| Approval workflow | No | No | No | No | Via ITSM integration | Ticket-oriented | No | **Yes, native** |
| Per-move verification + receipts | No | No | No | No | No | No | No | **Yes (HEAD-verified)** |
| Multi-account / multi-cloud | Bucket | Bucket | Bucket | Org | **Yes, multi-cloud (S3/GCS/Azure, 2026)** | Yes | Yes | No |
| Realized-savings accounting | n/a | n/a | n/a | n/a | Via CUR | Via CUR | List-price model only | Predicted-only, labeled |

Known IT facts used above (from AWS S3 pricing/docs): monitoring fee $0.0025/1,000 objects-month on monitored (≥128 KB) objects; sub-128 KB objects never monitored/tier-down; no retrieval fees within FA/IA/AIA; AA/DAA are opt-in buckets/objects with published archived-metadata overhead; no minimum storage duration. Independent break-even analyses place the IT-vs-manual-IA crossover near ~250 KB average object size — i.e., **IT is a serious, cheap default for most workloads; it wins on zero-ops automation and we cannot claim otherwise.**

Storage Class Analysis: observes access for 30+ days in age groups to inform lifecycle rules — insight only, no per-object recommendation, no execution. Storage Lens: org-wide dashboards incl. bytes downloaded (advanced metrics), no recommendation engine, no migration, no savings.

What competitors do better than us: universal coverage (every bucket just works), zero footprint, multi-account/multi-cloud, invoice truth (CUR), SOC 2 procurement posture, no customer engineering.
The actual gap: ** nobody operates per-object with billing-accurate economics + compliance eligibility + an approval gate + per-transition verification receipt + honest savings ledger.** Vantage's IT estimate even assumes the whole bucket lands in IA and does not subtract monitoring/transition fees; CloudHealth ignores transition costs entirely. Our per-object decision quality is real differentiation — but only if the object-level view is actually fed (Inventory + CloudTrail), which is our biggest missing component.

## 7. DIFFERENTIATION ANALYSIS

- v. IT: different function. IT optimizes **inside one class** with zero effort; we optimize **across classes, constrained by policy, with receipts**. The right posture: IT-compatible (engine already models IT incl. Poisson layer blend; recommend IT where it wins) + take the cases IT cannot do (state-legal tier constraints, sub-250 KB objects where IT math is negative, cross-class arbitrage with early-deletion fees, verified execution).
- v. Lifecycle: lifecycle cannot see access; we do (though only 30 days — a real limitation vs. months-long patterns).
- v. FinOps platforms: they are bucket-level + invoice-level; we are object-level + operation-level. Complementary, not substitutive. Position as the execution/governance layer that FinOps platforms lack for storage.

## 8. BACKEND REVIEW (production-approval stance)

**Approve (as-is, at small scale):** engine parity pinning; POST-only execute with GET-405; one-open-decision guard; Decimal discipline; explicit API methods over ANY (CORS preflights answered by the API); throttling; retained logs + alarms; presigned URL expiry 900 s; input validation on create; region portability.

**Blockers/correctness (verified in code):**
1. **P0 — schedule no-op:** `lambda_handler` routes only on `rawPath`/method; EventBridge `rate(1 day)` delivers a scheduled-event body → 404. Daily aggregation never runs in production; README and a template test imply otherwise. The test suite checks the schedule exists, not that the handler processes it.
2. **P1 — ledger double-count:** post-execute, `DocumentsTable.current_storage_class` is never updated (grep: no writer anywhere). Next aggregation writes the stale class into the aggregate; engine re-proposes the same migration; copy+HEAD "verify" succeeds again; `realized_savings` is added a second time for the same object. The 409 guard only blocks concurrent proposals, not post-verification re-proposals.
3. **P1 — propose-guard TOCTOU:** Query-then-put is not atomic; two concurrent proposals can both pass. Needs a `TransactWrite` conditioned on the document-index GSI (DynamoDB condition on a non-key index attribute is not directly possible → needs a per-document projection item or conditional put on a deterministic PK).
4. **P1 — no pagination:** `list_documents`, `fetch_all_documents` (Scan via pages internally — OK), but `list_decisions` (scan/query without NextToken) and `GET /documents` response (single Scan `Items` only) truncate beyond ~1 MB / 1 page. Fine at 500; wrong at product scale.
5. **P1 — CORS `AllowOrigins: ["*"]` + `AllowHeaders: ["*"]`** on a JWT API: workable for the dev SPA (Authorization header isn't auto-attached cross-origin), but any enterprise reviewer flags it. Pins needed.
6. **P1 — open self-signup:** no PreSignUp trigger → anonymous account creation → full API incl. execute rights (any authenticated user is an operator; no roles).
7. **P2 — broad exception 500s** swallow root causes (no structured logging); Lambda-level concurrency limit + SQS DLQ absent for aggregation; `update_state`/`get_decision` race benign.
8. **P2 — aggregates Lambda 60 s / 512 MB** with per-doc Query pool: ~2–3k documents ceiling (measured 10.3 ms/doc at 500 with pool); beyond that, Step Functions map or Athena over Inventory.

**Data-pipeline honesty:** aggregation is idempotent per run (rewrites snapshot), retries transient DynamoDB errors with bounded backoff + jitter, deterministic single `now` per run, sequential reference implementation kept for parity tests. Good engineering.

## 9. OPTIMIZER REVIEW (can it be trusted with real money?)

**Trustworthy:** deterministic; per-tier full breakdowns surfaced; policy conflicts flagged rather than forced; Policy B can never lose money by construction (current class always a candidate with no transition charge → negative savings floored to 0 as float-noise defense); ties resolve to staying put; minimum billable sizes and per-1,000 request economics present; early-deletion prorated fee anchored at `current_class_since` when present; IT modeled with a documented Poisson blend whose optimism for bursty workloads is disclosed; archived-object 40 KB overhead handled; retrieval wait surfaced informationally; independent audit script + A/B artifact agreement tests.

**Material weaknesses (ranked):**
1. **Single 30-day window.** Legal/fiscal workloads have annual, quarterly and case-triggered access. A document idle 29 days scores frequency 0 vs. one idle 31 days — but with one window you also can't see last year's single retrieval. Mitigation: CloudTrail gives continuous history cheaply; keep windows of 30/90/365.
2. **Whole-object retrieval assumption** — fine for legal documents, wrong for analytics workloads (range reads).
3. **Recency attenuation double-shrink risk:** forecast = freq×365 × 0.5^(days/30). A steady-monthly document last accessed 20 days ago is forecast at 63% — under stable periodic access, attenuation *under*forecasts; under decaying access it *over*forecasts (frequency already includes cold tail). Direction of net error is workload-dependent; honest answer: it's an unvalidated heuristic — and there is **no ground-truth validation loop** (nothing compares forecast vs realized accesses yet). This, not pricing, is the engine's biggest epistemic gap.
4. **Forecast isn't re-anchored after transition.** Post-migration access may rise (retrieval friction) creating a death spiral into cold tiers; no post-migration monitoring exists.
5. **Confidence absent.** A 500-object synthetic sample cannot produce decision-quality confidence; per-object savings need an uncertainty band (Poisson access CI is computable and would be cheap — recommended).
6. Minor: `GLACIER_IR` modeled with its own class (correct), one-zone IA not supported (fine for compliance personas), ETag/encryption of CopyObject assumes unencrypted-compatible, transition requests billed once but batch transitions (cheaper, ≤ as of 2024) not modeled.

**Verdict:** the math is defensible and unusually billing-faithful; the *epistemics* (single window, no validation loop, no confidence) are what a real FinOps reviewer will attack first. Fixing 1, 3, 5 converts "college model" into "defensible engine."

## 10. PRICING-MODEL REVIEW

Snapshot ap-south-1, checked 2026-09-10, refresh tool diffs every expected line against the Price List API and refuses bad writes — better discipline than two commercial competitors named above. Gaps: single-region single snapshot (multi-region customers need per-region snapshots); Deep Archive storage + restore-request lines cannot be verified from the API (documented); one-zone IA / Express / batch transitions absent; no EUR/INR display currency conversion (USD only). No hard-coded endpoints anywhere (verified). Acceptable; extend per-region at V1.

## 11. SCALE REVIEW

| Scale | Current architecture |
|---|---|
| 1k objects | Fine as-is (demo scale; ~10–20 s aggregation) |
| 100k | AggregationLambda timeout blown (100k Queries >> 60 s even pooled); DocumentsTable Scan OK but API listing breaks; proposer per-document model awkward (100k one-off decisions). **Not viable.** |
| 1M | Impossible (Lambda + per-object DynamoDB). Needs Inventory/Parquet + Athena + Step Functions. |
| 10M+ | Same, plus CloudTrail data events become the only affordable per-object access signal ($0.10/100k — ~$30/mo at 100M events; management-scale fine for legal corpora) |
| 100M+ | Segment-by-bucket/prefix; per-object recommender still CPU-cheap (pure math ~µs/object) — the cost is I/O, not compute |
| 1 TB → 1 PB | Storage-layer modeling identical (math per object); 1 PB at avg 1 MB = 1B objects → per-object store must shift to Parquet/Athena, not DynamoDB |
| Multi-bucket/account/region | None of it exists. |

**Production-scale recommendation (only what solves a real problem):** S3 Inventory (Parquet, daily) for object metadata+class; CloudTrail **data events** (targeted to selected buckets) for access events; partitioned Parquet in a logs bucket; Athena views = aggregation input (SQL replaces the 100k-DynamoDB-Query loop); Step Functions Distributed Map → Lambda fan-out (or Fargate job) for per-object optimization; decisions remain DynamoDB (decisions are human-scale, not object-scale — cap recommendation output to top-N savings-sorted batches); real-time ingestion (SQS/EventBridge pipes) **not needed** at first — daily Inventory + daily CloudTrail partition reads suffice.

## 12. SECURITY REVIEW

Present: JWT on every route but /config; presigned 900 s URLs; no secrets in frontend (PKCE, no client secret); AES256 + public-access-block; bucket in-account; least-privilege runtime policies; deploy preflight + reviewed least-privilege deployment policy (admin-attached); alarms.

Gaps an enterprise reviewer will raise: (1) open self-signup Cognito pool; (2) wildcard CORS; (3) JWT-authenticated = full operator (no RBAC; any user can execute S3 transitions); (4) no `approved_by` actor stamp (audit gap); (5) no CloudTrail-based audit page for decisions; (6) decisions Lambda role permanently holds s3:PutObject on the documents bucket (in-account acceptable; time-box for SaaS); (7) access-history table stores object IDs + timestamps only (low PII — good), but document metadata contains file names (can be case names — treat as customer data); (8) DynamoDB unencrypted-by-default? — tables here don't set SSE explicitly (AWS accounts default to owned-key encryption at rest now; check KMS CMK requirements for enterprise).

**Safest customer connection: external-ID cross-account roles (never long-lived keys).**
- Read role `TcoDiscovery-{tenant}`: trust policy = our management account only + `sts:ExternalId` = per-tenant secret; permissions: `s3:ListAllMyBuckets`, `s3:ListBucket`, `s3:GetInventoryConfiguration`, `s3:GetBucket*`, `cloudtrail:Lookup` + permission to create one S3 Inventory configuration + one CloudTrail data-event selector writing to THEIR logs bucket (data stays in their account).
- Write role `TcoExecute-{tenant}`: `s3:PutObject/GetObject` scoped to approved buckets only; assumable only through the approval service, STS session ≤ 15 min, every assumption logged to CloudTrail with the decision id in the session tag.
- Verification needs no extra perms (HEAD via write role).
- In-account deployment (current shape) is actually the *safer* onboarding for MVP — no credentials ever leave; adopt the external-ID roles only when building the SaaS control plane.

## 13. FRONTEND/UX REVIEW

Strengths (verified in code): LIVE vs EXPERIMENT separation is engineered, not decorative — dashboard estimates cost client-side from live metadata and deliberately shows no savings; experiment money lives only under EXPERIMENT badges; ledger echoes backend's honest basis string; two-step confirm on irreversible execute; signed-out states distinct from network failure; tier colors identity-based; pricing provenance pinned by vitest.

Weaknesses: (1) **Onboarding is absent** — no connect-AWS flow, no first-run guidance; the app assumes the stack is already fed. (2) A CTO's 30 seconds → dashboard shows experiment savings + live doc counts, but *their* money story ("what's my waste?") is the client-side estimate only — weak but honest. CFO: nothing invoice-anchored. (3) Auditor: strong provenance, missing actor identity. (4) Recommendations not bulk-actionable; propose is per-document. (5) No responsive audit done (desktop CSS-first); no a11y audit (aria labels in some states, untested). (6) Terminology mostly clean; "Policy A/B" is internal vocabulary in user-facing pages (Policies page explains it, fine for expert users, opaque to executives).

Persona verdicts: CTO — learns architecture, misses money. FinOps eng — can read, can't bulk-act. Cloud eng — can execute safely (the two-step + state machine is genuinely good). CFO — nothing. Auditor — trusts numbers, can't see who.

## 14. PRODUCT GAPS (P0/P1/P2/P3)

| Area | Current | Customer impact | Sev | Recommendation |
|---|---|---|---|---|
| Product positioning | College-demo shape | Blocks all go-to-market | P0 | Pivot to in-account governed optimizer (§THE ONE PRODUCT) |
| Existing-bucket ingestion | None | No customer can use it | P0 | Inventory + targeted CloudTrail data events |
| Schedule wiring | Broken (404 on scheduled events) | Features silently dead | P0 | Handle scheduled events + add integration test |
| Post-execute state sync | Missing | Double-counted savings = credibility death | P0 | Update DocumentsTable on verify + re-aggregation guard |
| Approval audit | No actor identity | Auditors reject | P1 | Stamp JWT `sub` on every state change + audit page |
| Scale | ~5k ceiling | Mid-market blocked | P1 | Step Functions + Athena path (V1) |
| Access history | Synthetic backfill everywhere | No claim about real-world accuracy is possible | P1 | Real access data (CloudTrail) + forecast-vs-realized validation |
| Confidence | None | No risk quantification for approvers | P1 | Poisson CI per forecast; show band |
| Auth/RBAC | Any user = operator | Enterprise blocker | P1 | Disable self-signup; admin-created users; operator/approver roles |
| CORS | Wildcard | Review findings | P1 | Pin to frontend origin |
| CI | None | Drift risk | P1 | GitHub Actions: pytest + vitest + sam validate + engine-parity |
| Pagination | Missing on listing routes | Data truncation | P1 | NextToken throughout |
| Propose guard | TOCTOU | Double-proposal | P1 | TransactWrite/conditional put |
| Frontend hosting | localhost-only | Demo-only product | P1 | CloudFront SPA + FrontendOrigin param |
| CUR/CE reconciliation | None | Savings credibility ceiling | P2 | Realized-vs-predicted diff report (V1) |
| Multi-region pricing | Single snapshot | Non-Mumbai customers | P2 | Per-region snapshots from Price List API |
| Multi-account | None | Enterprise blocked | P2 | External-ID roles (Enterprise V2) |
| Multi-cloud | None | Market ceiling | **P3 — do not build** | |
| Batch transitions modeling | Absent | Minor cost overestimate | P3 | |
| Notifications | None | Loop stalls silently | P2 | SNS digest on proposed/failed |

## 15. PRIORITY SUMMARY

**P0 (blocks viability):** bucket ingestion; schedule fix; post-execute state sync; product positioning decision.
**P1 (serious MVP):** audit stamps; CI; RBAC/signup; CORS; pagination; propose guard; forecast confidence; real-access validation loop; hosted frontend.
**P2:** CUR reconciliation; notifications; multi-region pricing; Step Functions scale-out.
**P3:** multi-cloud, batch transitions, one-zone IA, ML seasonality.

## 16. KILLER FEATURE

**"Receipts": every migration carries a machine-checkable receipt — features as-of, policy path, predicted cost both sides, HEAD verification result, actor, and later the CUR-anchored realized-vs-predicted diff.** No competitor (incl. IT) produces per-transition evidence; enterprises under compliance (legal, healthcare, finance) buy exactly this. Secondary killer: **explainable per-object "why" cards + confidence band** (counterfactual tier table the engine already computes) — turns a black-box recommendation into a reviewer artifact. Both are 70% built already; neither requires new architecture.

## 17. MVP (smallest genuinely sellable version)

**In-account deployment = "governed S3 tier optimizer appliance."**
1. Connect: deploy SAM stack; point at ONE existing bucket (Inventory Parquet source; targeted CloudTrail data events).
2. Discovery + aggregation replace the documents API as the data source (engine unchanged).
3. Recommendations table with why-cards + confidence + retrieval-risk; top-N by savings.
4. Approval queue (exists) → execute (exists) → HEAD verify (exists) → ledger (exists).
5. Hardening: schedule fix; state-sync fix; disable self-signup; pinned CORS; actor stamps; CI; pagination.
6. Scope cut: single account, single region, one bucket class (Standard-heavy), no SaaS, invoice anchoring deferred to V1.
Success metric: **verified transition with a receipt at a pilot customer** — not dashboard demos.

## 18. V1 (after MVP)

Step Functions scale-out (100k–1M objects); CUR/CE realized-savings reconciliation; notification digests; multi-region pricing snapshots; CloudFront-hosted frontend + custom domain; forecast-vs-realized validation report (the credibility engine); schedule + weekly policy review cadence.

## 19. ENTERPRISE V2

Org onboarding via external-ID roles; multi-account/region; policy packs (tag/prefix-driven eligibility); ITSM (ServiceNow/Jira) approval hooks; RBAC + SSO (Identity Center); SOC 2 Type II; per-tenant audit export; realized-savings dashboard with invoice anchoring.

## 20. BUSINESS MODEL (estimates, clearly labeled as estimates)

- **MVP: in-account, per-deployment license** — flat annual fee for the deployment + updates (e.g., $5k–$25k/yr depending on managed-GB; assumption-based estimate). No savings-share complexity at this stage; zero data leaves the customer's account (it doesn't in the current design at all — a genuinely sellable property).
- **V1 option: % of verified realized savings** (10–20% of first-year verified, per receipts; capped). Only sellable once CUR anchoring exists; percentage-of-savings without verification would be selling the same predicted numbers competitors give free.
- **Enterprise V2: SaaS + per-TB-managed** (e.g., $1–3/TB-month, assumption-based) with external-ID onboarding.
Willingness-to-pay anchor (estimate): a customer with 200 TB Standard-heavy storage wastes $50k–$100k+/yr; a tool that *verifiably* moves even 30% of that — with governance — plausibly supports mid-four-figures-per-month pricing. Do not quote these numbers externally.

## 21. WHAT NOT TO BUILD

Multi-cloud (GCS/Azure) — Cloudability ships it; you cannot win there and it fragments the engine. A per-object SaaS control plane before in-account proves out. OpenSearch/Aurora/real-time streaming/Kafka — no current problem they solve. LLM "chat with your storage" — gimmick vs. receipts. Invoice reconciliation before verified transitions exist. ML seasonality before real access data exists (garbage-in amplification). Kubernetes/microservice split — the Lambda boundaries are correct. Automatic unpiloted execution mode — violates the product's core trust thesis. Another dataset or experiment script — the marginal artifact adds nothing to sale-ability.

## 22. FINAL SCORECARD

- Technical architecture: **7/10** (clean, testable, real invariants; schedule bug + scale ceiling)
- Optimization engine: **7.5/10** (billing-faithful, honest; epistemics weak: one window, no validation, no confidence)
- Scalability: **4/10** (works to ~5k; nothing beyond a single Lambda)
- Security: **5.5/10** (good in-account defaults; open signup, wildcard CORS, no RBAC, no actor audit)
- Product value: **4.5/10** (kernel valuable; connectivity to real customers = zero)
- UX: **6.5/10** (provenance discipline exemplary; onboarding and bulk action absent)
- Differentiation: **5.5/10** (receipts + eligibility gating are real; invisible until fed real buckets)
- Production readiness: **3/10** (P0s, no CI, localhost demo)
- **Overall product readiness: 4/10 — a kernel worth building on; the wrapper is what's missing.**

## 23. EXACT NEXT STEPS (in order)

1. Decide positioning (below) — today.
2. Fix schedule no-op (handler branch for scheduled events + integration test) — 0.5 d.
3. Fix post-execute state sync (update DocumentsTable.current_storage_class on verified; block re-proposal while aggregate stale) — 0.5 d.
4. CI workflow (pytest + vitest + sam validate) — 0.5 d.
5. Security hardening: disable self-signup, pin CORS, stamp `approved_by` — 1 d.
6. Pagination + TransactWrite propose guard — 1 d.
7. Inventory + targeted CloudTrail data-event ingestion (read-only discovery) feeding existing aggregates — 1–2 wk. *This is the make-or-break component.*
8. Why-cards + confidence band UI + bulk propose — 1 wk.
9. Receipts page + validated forecast-vs-realized report — 1 wk (needs 30 days of real access data collection first — start the clock now).
10. CloudFront-hosted frontend — 2 d.

---

## THE ONE PRODUCT WE SHOULD BUILD

**Positioning: "the policy-aware, evidence-first storage tier optimizer for compliance-bound S3 estates" — the governed execution layer that Intelligent-Tiering and lifecycle rules deliberately are not.**

Working name: **TierLedger** (alternatives: ColdProof, Receipted, SpendSentry).

One-paragraph definition: TierLedger is an in-account S3 appliance that connects to an organization's existing buckets through S3 Inventory and targeted CloudTrail data events (read-only), continuously builds billing-accurate per-object cost projections across all storage classes under the organization's own compliance eligibility policy, presents explainable recommendations — each with its cost breakdown, confidence, retrieval-latency risk, and policy context — and, only after a named human's approval, performs the tier transition and returns a machine-checkable receipt: features as-of, predicted costs, HEAD-verified result, actor identity, and later the CUR-anchored realized-vs-predicted diff. It never asks for credentials, never moves a byte unapproved, never reports a saving it cannot evidence, and is honest on day one about what is live, modeled, or unverified — which is exactly the trust posture its buyers (legal, healthcare, finance, public-sector) are forced to care about.