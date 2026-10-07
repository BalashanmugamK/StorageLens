# Intelligent Cloud Storage Cost Optimizer

An AWS-based system that analyzes cloud object access patterns and recommends cost-effective storage classes while considering storage, request, retrieval, and transition costs.

## Problem

Organizations often keep large collections of documents in frequently accessed storage even when many objects become rarely accessed over time. This can result in unnecessary storage costs.

This project explores an access-aware approach to cloud storage optimization, demonstrated with a **synthetic legal-document storage use case** (no real personal data), plus a **real public legal-document corpus** (see [Data provenance](#data-provenance)).

## Getting started — walkthrough for the person using the product

You do not need to understand the code to use StorageLens. From zero to a working product, there are five stages. Every command below runs from the repository root.

### Stage 1 — prerequisites (once per machine)

- An **AWS account you own**.
- **AWS credentials** configured (`aws configure` or an SSO/admin profile) — StorageLens deploys into *your* account; nothing is shared with any external service.
- **AWS SAM CLI** installed ([guide](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html)).
- **Python 3.13** (for the backend/tests) and **Node.js 20** (for the web console).

### Stage 2 — deploy into your AWS account

```bash
cd infrastructure
sam build
cd ..
python scripts/check_deployment_permissions.py   # read-only: tells you if your AWS identity can deploy
sam deploy --template-file infrastructure/.aws-sam/build/template.yaml \
  --stack-name intelligent-storage-cost-optimizer \
  --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM --resolve-s3
```

Then one command wires the rest up (reads the stack's own outputs, writes `frontend/.env`, prints the sign-in URL):

```bash
python scripts/deploy_and_configure.py
```

At this point the product exists in your account: the API, the database tables, the web console hosting, auth, alarms.

### Stage 3 — create your users (admins create accounts; there is no public signup)

Sign-up is deliberately admin-only so no anonymous account can reach your API. Create users with:

```bash
aws cognito-idp admin-create-user --user-pool-id <UserPoolId output> \
  --username alice@yourcompany.com \
  --user-attributes Name=email,Value=alice@yourcompany.com Name=email_verified,Value=true
# make someone an approver (can approve/reject/execute migrations):
aws cognito-idp admin-add-user-to-group --user-pool-id <UserPoolId> \
  --username alice@yourcompany.com --group-name Approvers
```

Regular (view-only) users simply aren't added to `Approvers` — they see the console read-only, exactly as RBAC intends.

### Stage 4 — get data in (two ways)

- **Your own bucket** (the real product path): in the console's **Onboarding** page, paste your bucket name and follow the wizard — it runs a read-only preflight, then imports an [S3 Inventory](https://docs.aws.amazon.com/AmazonS3/latest/userguide/storage-inventory.html) of your objects and parses your [S3 server access logs](https://docs.aws.amazon.com/AmazonS3/latest/userguide/ServerLogs.html) into access history. StorageLens only ever reads.
- **Demo data** (zero setup):

```bash
python scripts/load_synthetic_data.py --stack-name intelligent-storage-cost-optimizer --region ap-south-1 --purge-access-history
```

Then on the console, run the aggregation once (dashboard or `POST /aggregates/run`).

### Stage 5 — use it (the daily workflow)

1. **Open the console** (see [Frontend](#frontend-storagelens) — `npm run dev`, sign in at the hosted-UI it links to).
2. **Dashboard** shows fleet aggregates: storage-class distribution, access patterns, projected baseline vs optimized annual cost.
3. **Approvals page** → *Propose decisions* — the engine models every document's 12-month cost across six storage classes and proposes the cheapest safe migration with predicted savings (an interval, not a guess).
4. **An approver clicks Approve, then Execute** — StorageLens actually moves the object in S3, verifies with a HEAD request, and only then records the saving into the **ledger**.
5. **Reconciliation page** — run `POST /reconciliation/run` to compare the model's forecast against **your real AWS bill** (Cost Explorer) per storage class, monthly. This is the trust signal: predicted vs actually-realized.
6. **On-Call page** — create on-call shift windows; alarms page PagerDuty automatically (see [On-Call & PagerDuty](#on-call--pagerduty) for the one-time credential step).
7. The **Experiments** page replays the recorded optimization benchmarks; **Pricing** shows the exact rates the engine used and their freshness.

That's the whole product: *connect a bucket → see what it costs → approve a safer tier → execute it → prove the saving on the bill*.

## Architecture

```
                      ┌────────────────┐  Cognito JWT (hosted UI, PKCE)
  React frontend ────►│ API Gateway    │──────────────────────────────┐
  (StorageLens)       │ (DocumentsHttp │                              │
                      │      Api)      │                              ▼
                      └───────┬────────┘                  ┌──────────────────┐
             /documents*  /aggregates/run                │ Cognito UserPool │
             /decisions* /ledger  /config                └──────────────────┘
                             ▼
              ┌───────────────────────────┐
              │      Lambda python3.13    │
              │  DocumentsFunction        │  AggregatesFunction
              │  create/list/get/download │  aggregate 30-day features
              │  ─────────────────────────│
              │  DecisionsFunction        │  propose/approve/reject →
              │  execute (S3 copy)        │  verify → realized savings
              └──────┬───────────┬────────┘
                     │           │
     ┌───────────────▼───┐   ┌───▼────────────────────┐
     │  DynamoDB         │   │ DynamoDB                │
     │  DocumentsTable   │   │ AccessHistoryTable      │
     │  DecisionsTable   │   │ (raw access events)     │
     └───────────────────┘   └────────────┬────────────┘
             │                            │ scan/query
     ┌───────▼──────────┐                 │
     │  S3 bucket       │                 ▼
     │  DocumentsBucket │   ┌─────────────────────────┐
     │  (object files)  │   │ DocumentAggregatesTable │
     └──────────────────┘   └────────────┬────────────┘
                                         │
                                         ▼
                              optimization/ (pure Python engine)
                              storageclass recommendation + costs
                              (byte-identical copy bundled into
                               the decisions Lambda)
```

### AWS resources (all created by CloudFormation/SAM)

| Resource | Type | Notes |
|---|---|---|
| `DocumentsBucket` | S3 Bucket | AES256 encryption, public access blocked |
| `DocumentsTable` | DynamoDB | PROVISIONED 1 RCU/1 WCU, PK `document_id` |
| `AccessHistoryTable` | DynamoDB | PAY_PER_REQUEST, PK `document_id`, SK `access_timestamp` |
| `DocumentAggregatesTable` | DynamoDB | PAY_PER_REQUEST, PK `document_id` |
| `DecisionsTable` | DynamoDB | PAY_PER_REQUEST, PK `decision_id`; GSIs `state-index`, `document-index` |
| `ReconciliationsTable` | DynamoDB | PAY_PER_REQUEST, PK `scope` (`"reconciliation"`), SK `ran_at` |
| `OncallShiftsTable` | DynamoDB | PAY_PER_REQUEST, PK `shift_id`; GSIs `role-start-index`, `engineer-start-index` |
| `OncallNotificationsTable` | DynamoDB | PAY_PER_REQUEST, PK `notification_id` (SNS MessageId); GSIs `time-index`, `alarm-index` |
| `PagerDutySecret` | Secrets Manager | Created EMPTY (no inline value); operator fills it post-deploy with the PagerDuty credentials |
| `DocumentsFunction` | Lambda | Python 3.13, documents API |
| `AggregatesFunction` | Lambda | Python 3.13, aggregation API (daily `rate(1 day)` schedule) |
| `DecisionsFunction` | Lambda | Python 3.13, the approval → execute → verify loop |
| `ReconciliationFunction` | Lambda | Python 3.13, model-vs-bill reconciliation (Cost Explorer + engine, daily `rate(1 day)` schedule) |
| `OnCallFunction` | Lambda | Python 3.13, shift CRUD + coverage, alarm→PagerDuty paging (SNS subscriber), daily override reconciler (`rate(1 day)`) |
| `UserPool` / `SpaClient` / `UserPoolDomain` | Cognito | Hosted-UI sign-in (auth code + PKCE) for the SPA; JWT authorizes every API route except `GET /config` |
| `DocumentsHttpApi` | HTTP API | `$default` stage, Cognito JWT authorizer, throttled |
| Log groups + CloudWatch alarms | Logs/Metrics | 30–90+ day retention, error/5XX alarms to an SNS topic |
| `deployment-policy.json` | (file, not resource) | Least-privilege policy template for the deployment identity; admin-reviewed, placeholder-based |

**All resource names (bucket, tables, functions, API URL) are generated dynamically by CloudFormation in whatever account/region you deploy to. Never hard-code them**; read them from `sam list resources` / CloudFormation outputs / the stack description.

## Repository Structure

```
backend/lambdas/documents/   Document API Lambda
backend/lambdas/aggregates/  30-day aggregation Lambda
backend/lambdas/decisions/   Approval / execute / verify Lambda
backend/lambdas/decisions/engine/  byte-identical copy of optimization/ (pinned by tests/test_engine_parity.py)
backend/lambdas/reconciliation/  Model-vs-bill reconciliation Lambda (Cost Explorer)
backend/lambdas/ingest/      S3 inventory / access-log import Lambda
backend/lambdas/oncall/      On-call shifts + PagerDuty paging / sync Lambda
optimization/                Pure-Python optimizer (models, costs,
                             constraints, baseline, adapters)
data/synthetic/              Deterministic synthetic test data
data/real/                   Real public-legal corpus (GovInfo, see Data provenance)
scripts/                     build/load/validate/audit/refresh tools,
                             deployment preflight + configure
infrastructure/template.yaml AWS SAM template
tests/                       pytest suite (244 tests)
frontend/                    StorageLens (React + Vite): dashboard,
                             onboarding/import, documents, reconciliation,
                             approvals, on-call, pricing, experiments
docs/architecture/           Architecture notes
.github/workflows/ci.yml     CI: backend pytest, frontend lint/test/build,
                             sam validate + template hardening
```

## API Endpoints (live stack)

Every route except `GET /config` sits behind the Cognito JWT authorizer — requests carry `Authorization: Bearer <ID token>` from the SPA's hosted-UI session.

| Method | Path | Behavior |
|---|---|---|
| GET | `/config` | Public: Cognito pool/client/domain ids + region for the SPA's sign-in flow |
| POST | `/documents` | Create metadata + presigned S3 upload URL (needs `file_name`, `file_size`, `content_type`) |
| GET | `/documents` | List all documents |
| GET | `/documents/{document_id}` | Get one document |
| GET | `/documents/{document_id}/download` | Record a DOWNLOAD access event + presigned download URL |
| GET | `/documents/{document_id}/access-stats` | All-time count, last access, 30-day count |
| POST | `/aggregates/run` | Aggregate all documents into `DocumentAggregatesTable` (also invoked nightly by the EventBridge schedule) |
| GET | `/aggregates/stats` | Live pipeline state: aggregate coverage, missing aggregates, storage-class distribution, last run |
| POST | `/decisions` | Recompute the recommendation with the real engine and record a `proposed` migration (409 when there is nothing to migrate, the aggregate is stale, or one is already open) |
| POST | `/decisions/propose-batch` | Run the engine over every live aggregate and record proposals in bulk (body `{max}`, default 25, cap 100); skips documents that already have an open decision, are missing, or have stale aggregates |
| GET | `/fleet/projection` | Engine run over all live aggregates: baseline vs optimized projected annual cost per policy (`?policy=A\|B`, default B), migration list, tier allocation, staleness counts |
| GET | `/decisions` | List decisions (`?state=proposed` filters via the `state-index` GSI) |
| GET | `/decisions/{decision_id}` | One decision |
| POST | `/decisions/{decision_id}/approve` | `proposed → approved` |
| POST | `/decisions/{decision_id}/reject` | `proposed → rejected` |
| POST | `/decisions/{decision_id}/execute` | `approved → verified/failed`: copy the object onto itself with the approved storage class, then verify with a HEAD request (GET returns 405 — no prefetch may fire a transition) |
| GET | `/ledger` | Realized-savings ledger over all `verified` decisions + its honest basis string |
| POST | `/imports/check` | Onboarding preflight — bounded reads answer "will an import work on this bucket?" before importing anything: reachability (the same grants the imports use), inventory presence (a given manifest key verified + column-sniffed, or bounded discovery of `schema.csv` candidates), and a prefix sample where up to 3 objects' first KiB is parsed for S3 access-log format. Nothing is imported and no bucket state changes |
| POST | `/imports/inventory` | Import document rows from an S3 Inventory manifest the product never uploaded: body `{bucket, key}` (the manifest `schema.csv`/`.csv.gz` object), + optional `{document_state}`. Deterministic ids (uuid5 of `s3:{bucket}/{key}`) make re-imports idempotent upserts; caps at 200,000 rows per call and reports skipped/unsupported-class counts |
| POST | `/imports/access-logs` | Turn successful GETs in S3 server access logs into real DOWNLOAD access events with the logs' own timestamps: body `{bucket, prefix?}`. Paginates up to 200 log files per call; reports per-file parse failures |
| POST | `/reconciliation/run` | Compare the model against the actual bill: Cost Explorer `UnblendedCost` for the S3 service grouped by usage type over complete months (body `{months?}`, default 3, cap 12) vs the engine's storage prediction over the live aggregate snapshot — variance per storage class per month. Non-storage line items (requests, transfer, early delete) and unmapped storage classes are reported, never folded in. Returns the report (404-free; 502 with the CE error when billing data can't be read) |
| GET | `/reconciliation/result` | The latest recorded reconciliation report (404 until one has run) |
| GET | `/reconciliation/history` | Every past reconciliation run, newest first |
| GET | `/oncall/me` | Who the caller is for the on-call pages (`{principal, can_manage}`; `can_manage` = Approvers member) |
| GET | `/oncall/current` | Who is on call right now: primary/secondary shift windows, overlap list, `coverage: covered\|none` |
| GET | `/oncall/shifts` | List shift windows (`?role` default primary, `?engineer`, `?from`/`?to`, paginated) |
| POST | `/oncall/shifts` | Create a shift window (max 7 days per shift; Approvers only) — best-effort push into PagerDuty overrides follows automatically |
| PUT | `/oncall/shifts/{shift_id}` | Replace a shift window (Approvers only) |
| DELETE | `/oncall/shifts/{shift_id}` | Delete a shift and remove its PagerDuty override (Approvers only) |
| POST | `/oncall/sync` | Reconcile now: diff our future shifts against 30 days of PagerDuty overrides, add missing + remove stale |
| GET | `/oncall/notifications` | Audit trail of every paging decision: which alarm, state change, PagerDuty result (`success`/`skipped_*`/`failed`), routing, and who was on call at page time |

## On-Call & PagerDuty

Alarms previously published only to an SNS topic — with no subscriber
attached, they effectively went to /dev/null. The on-call feature makes
them actionable:

- **Shift windows are manual and admin-managed** (Approvers group) in
  DynamoDB: engineer, role (`primary`/`secondary`), UTC start/end (max
  7 days per window). Coverage resolution is half-open
  (`start_at <= now < end_at`); per role the newest `start_at` wins.
- **CloudWatch alarms page PagerDuty**: the on-call Lambda subscribes
  to the alarms topic. Every state change becomes a PD Events v2 call
  — `ALARM` triggers a critical incident, `OK` resolves the matching
  incident (the dedup key is the alarm ARN), `INSUFFICIENT_DATA` is
  audit-only. When no shift covers the moment, the page still fires
  with `oncall_coverage: "UNROUTED"` — PagerDuty is the human fallback.
- **Rotation sync is two-way** in the sense that our shifts are the
  source of truth and land in PagerDuty as schedule *overrides*
  (per-shift `pagerduty_user_id` gives the PD user reference;
  shifts without it record `skipped_missing_pd_user_id`). A daily
  EventBridge schedule (plus the admins' `POST /oncall/sync`) diff the
  next 30 days of overrides against our future shifts and converge
  them. Acknowledgment and escalation are owned by PagerDuty's own
  escalation policy — this stack deliberately does not rebuild them.
- **Every paging decision is audited** in `OncallNotificationsTable`
  (idempotent on the SNS `MessageId`): alarm, state, `pd_status`
  (including `skipped_no_secret` when credentials are not yet
  configured), routing, and who was on call at page time.
- **PagerDuty credentials live in a Secrets Manager secret the
  template creates empty** — CloudFormation never holds a credential.
  Fill it right after the first deploy:

```bash
aws secretsmanager put-secret-value \
  --secret-id <PagerDutySecretArn from the stack output> \
  --secret-string '{"routing_key":"<Events v2 routing key>",\
"api_token":"<REST API token with schedules.write>",\
"from_email":"<email of a PD user allowed to edit the schedule>"}'
```

- **Prerequisites on the PagerDuty side** (one-time, an admin does
  this once): a service for the stack (gives the Events v2 routing
  key), a schedule with its escalation policy (gives the schedule id
  for the `PagerDutyScheduleId` parameter), a REST API token, and the
  PD user ids for each on-call engineer. `PagerDutyScheduleId` is
  empty at deploy time — pass it (`--parameter-overrides
  PagerDutyScheduleId=...`) or update the stack later; with it empty,
  every sync is recorded as `skipped_no_schedule` instead of failing.

## Connecting an existing bucket

The ingest routes are how the optimizer sees storage it never uploaded — the
bridge from demo data to a customer's real bucket. The **Onboarding** page in
the console turns these steps into a wizard: paste a bucket name, it runs the
preflight (`POST /imports/check`) and offers the discovered manifest, then
walks through imports and aggregation. The API steps, for scripting:

1. **Check**: `POST /imports/check` with `{bucket, key?, prefix?}` — bounded
   reads (nothing imported, nothing changed) reporting reachability, manifest
   presence + column fit, and whether anything under the prefix parses like
   an access log.
2. **Inventory**: enable an [S3 Inventory](https://docs.aws.amazon.com/AmazonS3/latest/userguide/storage-inventory.html)
   on the bucket (or point the import at any inventory-style CSV with the
   columns `Key, Size, LastModifiedDate, StorageClass`), then call
   `POST /imports/inventory` with `{bucket, key}` for the manifest object.
   Each row becomes a document in the standard tables.
3. **Access history**: enable
   [S3 server access logging](https://docs.aws.amazon.com/AmazonS3/latest/userguide/ServerLogs.html)
   on the same bucket, then call `POST /imports/access-logs` with
   `{bucket, prefix}`. Successful GETs become the DOWNLOAD events the
   aggregation's recency/frequency features read.
4. **Pipeline**: from there everything is the normal flow —
   `POST /aggregates/run` aggregates the imported documents, the engine and
   `GET /fleet/projection` treat them the same as uploaded ones.

The Ingest Lambda only ever gets read-only `s3:GetObject`/`s3:ListBucket` on
the customer's bucket (`Resource: "*"` scoped by role trust) plus
`PutItem`/`BatchWriteItem` on the two tables. Both imports are idempotent and
both responses carry the exact counts and basis string — nothing is silently
dropped or silently guessed.

## The approval → execution → verification loop

The analysis half produces recommendations; the loop's write half turns one into a real, verified S3 storage-class transition:

1. **Propose** — a reviewer clicks *Propose migration* on a document (or `POST /decisions`), or bulk-proposes with *Propose decisions* on the Approvals page (`POST /decisions/propose-batch`). The decisions Lambda re-runs the engine over the document's latest aggregate and stores the forecast with the exact features it consumed (`features_as_of`).
2. **Approve / reject** — a `proposed` decision is human-gated; approval stamps the decision; execution without approval is refused.
3. **Execute** — `approved` only. The Lambda performs an S3 CopyObject of the object onto itself with the target `StorageClass`, the standard way to change a class in place.
4. **Verify** — immediately after the copy, a HEAD request reads the object's actual storage class; only a matching HEAD moves the decision to `verified` and records `realized_savings`. Intelligent-Tiering objects may report `STANDARD` on HEAD and are accepted accordingly; any mismatch lands the decision in `failed` with the reason.
5. **Ledger** — `GET /ledger` sums realized savings across verified decisions and states its basis plainly: *predicted annual savings carried by decisions whose S3 copy was verified via HEAD — not invoice data*.

One open decision per document is enforced (a duplicate proposal while another is proposed/approved gets a 409), so a document can never be migrated twice by two concurrent approvals. After a verified execution the Lambda also updates the document's `current_storage_class` in `DocumentsTable`; until the next aggregation run re-records the aggregate, any new proposal against the outdated aggregate is refused with a 409 (`aggregate is stale`) — a single migration can never be double-counted.

**Engine parity guarantee:** Lambda deploys a single directory, so `backend/lambdas/decisions/engine/` holds a byte-identical copy of `optimization/`. `tests/test_engine_parity.py` pins the two sides to the same bytes and proves the copy runs standalone — live recommendations can never silently drift from the offline experiments.

## Deployment

Prerequisites: AWS CLI configured with credentials for **your own account**, AWS SAM CLI, Python 3.13, and a default region (set in `samconfig.toml`; currently `ap-south-1` — change it there or pass `--region` to deploy to any other region).

### Deployment permission preflight (run before `sam deploy`)

```bash
python scripts/check_deployment_permissions.py [--stack-name <name>] [--region <region>] [--json]
```

Read-only check that the current deployment identity appears able to deploy this stack. It identifies the identity/account/region at runtime, derives the required permissions from the actual template + `samconfig.toml` (both functions create CloudFormation-managed execution roles, so `iam:CreateRole` is required on the **deployment identity** — it is distinct from the DynamoDB/S3 permissions granted to the **execution roles** for the app's runtime), and verifies them with the IAM Policy Simulator, falling back to a review of the identity's own policy documents, or reporting clearly when IAM inspection itself is not permitted. Exit codes: `0` prerequisites complete, `1` missing permission(s) found, `2` identity/config resolution failure, `3` unable to verify (IAM inspection denied, nothing known-missing).

A least-privilege deployment policy for **the deployment identity** is maintained at `infrastructure/deployment-policy.json` (`{{ACCOUNT_ID}}`/`{{REGION}}`/`{{STACK_NAME}}` placeholders — substitute, review, and attach only by an administrator's explicit action; nothing in this repo ever modifies IAM automatically). Note: deploying with the account **root** user technically works but is not recommended — create a least-privilege deployment identity instead. SCPs, session policies, and resource policies are outside the preflight's visibility.

### Portability

The project is machine- and account-independent:

- No AWS account IDs, credentials, or physical resource names are hard-coded anywhere. All Lambda resource references and stack `Outputs` come from CloudFormation intrinsics (`!Ref`/`!GetAtt`/`!Sub`).
- Deploying from another machine, AWS account, or region produces its own generated resource names. Scripts (`scripts/load_synthetic_data.py`, `scripts/validate_*.py`, `scripts/audit_all_optimizations.py`) discover every bucket/table via CloudFormation stack outputs, never literal names.
- Lambda code resolves region from the runtime's `AWS_REGION`; there are no hard-coded regional endpoints.
- Generated data files (`data/synthetic/*_500.json`) were produced from the deterministic generator in this repository and contain no machine-specific paths.

Regenerating data from a clean checkout reproduces the same deterministic content (fixed seeds); event timestamps anchor at generation time.

```bash
sam validate -t infrastructure/template.yaml
sam build -t infrastructure/template.yaml
sam deploy
```

`samconfig.toml` uses `resolve_s3 = true`, so SAM creates/reuses a managed deployment bucket in the **current** account automatically — no bucket name is hard-coded.

Note: the stack name is `intelligent-storage-cost-optimizer`.

### Important environment variables

These are set automatically by the template on the Lambdas (`!Ref` to generated resources):

| Variable | Consumed by | Meaning |
|---|---|---|
| `DOCUMENTS_BUCKET` | DocumentsFunction, DecisionsFunction | Generated S3 bucket name |
| `DOCUMENTS_TABLE` | All three | Generated documents metadata table |
| `ACCESS_HISTORY_TABLE` | DocumentsFunction, AggregatesFunction | Generated access-history table |
| `AGGREGATES_TABLE` | AggregatesFunction, DecisionsFunction | Generated aggregates table |
| `DECISIONS_TABLE` | DecisionsFunction | Generated decisions table |
| `USER_POOL_ID` / `CLIENT_ID` / `AUTH_DOMAIN` | DocumentsFunction | Fed to `GET /config` for the SPA's sign-in flow |

The stack's CloudFormation Outputs carry the same values the frontend needs (`UserPoolId`, `SpaClientId`, `CognitoDomain`, `FrontendOrigin`, plus the API endpoint); read them with `sam list outputs` / the stack description rather than guessing. `scripts/deploy_and_configure.py` does that reading for you and writes `frontend/.env` from the stack's own outputs — after a deploy, one command finishes the setup:

```bash
python scripts/deploy_and_configure.py [--stack-name NAME] [--region REGION]
```

## Frontend (StorageLens)

React + Vite SPA in `frontend/`. Dashboard, document inventory + upload, a per-document modeled recommendation, onboarding/import wizard for existing buckets, reconciliation history (model vs real bill), the Approvals page that drives the loop above, the On-Call console (coverage, shifts, alarms audit), and the offline experiment explorer + pricing view.

```bash
cd frontend && npm install
cp .env.example .env      # set VITE_API_BASE_URL to the deployed API endpoint
npm run dev               # dev server on :5173 — must match the Cognito client's
                          # CallbackURLs/LogoutURLs (FrontendOrigin, default
                          # http://localhost:5173; override the template
                          # parameter when deploying for another origin)
npm run test              # vitest (includes a pricing-provenance pin against
                          # the bundled experiment meta)
```

Sign-in uses Cognito's hosted UI with the authorization-code + PKCE flow implemented in `frontend/src/services/authLogic.ts` (no Cognito SDK dependency), session in `sessionStorage`, silent refresh on 401. Every signed-out state in the UI is labeled as *signed out*, never as a network failure.

## Data provenance

The demo corpus is not arbitrary files — it is real, public, legally-shareable government material:

- **Real dataset (`data/real/us_courts/`, `data/real/diversified/`)** — public-domain court documents from the **[GovInfo](https://www.govinfo.gov/) API** (the U.S. Government Publishing Office). `scripts/build_real_dataset.py` discovers document granules via the free GovInfo Search API (a free API key from <https://www.govinfo.gov/api-signup> is read from `GOVINFO_API_KEY` and never written to disk), selects 500 documents deterministically with the default `USCOURTS` collection (any GovInfo collection works via `--collection/--court-code/--query`), validates each candidate with HTTP HEAD (`application/pdf`, known content-length), enforces per-document (≤3 MB) and cumulative (≤300 MB) limits, downloads the PDFs from `govinfo.gov/content/pkg/...`, and writes `manifest.json`. A non-GovInfo corpus with the same manifest schema loads via `load_real_data.py --dataset-dir`. GovInfo material is U.S. federal government work in the public domain; nothing the pipeline ingests is user data or personal data.
- **Synthetic dataset (`data/synthetic/`)** — deterministic (fixed seeds, reference timestamp `2026-09-12T00:00:00Z`) and mimics a law firm: 500 documents with file names/content types, practice-area metadata, mixed storage classes, document states (ACTIVE/CLOSED/ARCHIVED/null), and mixed 30-day access patterns (0 to 300 downloads per document, ~15,200 raw events total).

### Regenerating the synthetic data

```bash
# 1. Generate (local JSON, no AWS)
python scripts/generate_synthetic_data.py

# 2. After a successful deploy, load (S3 + DynamoDB)
python scripts/load_synthetic_data.py \
    --stack-name intelligent-storage-cost-optimizer \
    --region ap-south-1 \
    --purge-access-history
# optional: --limit 50 (first N docs only) or --skip-s3 (events/metadata only)
# --purge-access-history clears AccessHistoryTable first so re-seeding
# never double-counts events.
```

Data files (all under `data/synthetic/`):

- `workload_500.json` — seeded aggregate workload (the optimizer's answer key; 10-field schema)
- `workload_500_results.json` — pre-computed optimizer/baseline experiment
- `documents_500.json` — live-pipeline document records (object keys, file names, metadata)
- `access_events_500.json` — raw DOWNLOAD events inside the 30-day window

Caveat: synthetic metadata `file_size` can be larger than the tiny uploaded placeholder payload; the metadata drives the cost model. Uploaded objects land in S3 Standard; `DocumentsTable.current_storage_class` reflects the synthetic class for optimizer testing.

## The 30-Day Observation Window

All access-history aggregation uses a fixed **30-day window**: the aggregator queries `AccessHistoryTable` with `access_timestamp >= now - 30 days`, counts DOWNLOAD events, and computes `access_frequency = access_count / 30`, matching `optimization/access.py`.

## How the optimizer decides

`optimization/` projects each document's annual cost per candidate storage class from the snapshot in `optimization/pricing.json` (whatever region the snapshot recorded — refresh with `scripts/refresh_pricing.py` to re-target) and recommends the cheapest. The cost model covers storage, retrieval fees, GET request charges, and lifecycle transition requests — **per 1,000 objects** — and includes the billing details that matter for small or young legal files:

- **Minimum billable object size** — 128 KB on Standard-IA/Instant-Retrieval, 32 KB on Glacier Flexible/Deep Archive, regardless of the real object size.
- **Archived-object overhead** — Glacier Flexible/Deep Archive add 40 KB of metadata per object (8 KB billed at Standard rates + 32 KB at the archive rate).
- **Early-deletion fee** — moving out of a class inside its minimum storage duration (SIA 30 d, Instant-Retrieval/Flexible 90 d, Deep Archive 180 d) bills the remaining days prorated on the current tier. `destination_early_exits_modeled` is `false`: fees for leaving the *destination* tier are not modeled.

The access forecast is `30-day frequency × 365`, attenuated by an exponential recency weight (`0.5 ** (days_since_last_access / 30)`) — the system extracts recency-related features, while the current cost model bases its projected access load primarily on the observed 30-day frequency. Restore waits (≈5 h Flexible, ≈12 h Deep Archive standard) are surfaced **informationally** (`retrieval_time_hours`); they do not affect cost.

### Eligibility policy (A vs B)

Document state constrains the candidate classes (ACTIVE → Standard/SIA/Instant-Retrieval; CLOSED → SIA/Instant-Retrieval/Flexible; ARCHIVED → Instant-Retrieval/Flexible/Deep Archive; unknown state → all).

- **Policy B (default, cost-safe)** — the current class always joins the candidate set, so the engine never recommends a migration that *increases* projected cost. When the current class is not eligible for the document's state, the result carries `policy_conflict: true` instead of silently forcing a move.
- **Policy A** — strictly state-constrained argmin: it may recommend a migration inside the eligible set even when projected savings are negative (e.g. an ARCHIVED document with heavy retrieval load where holding SIA is cheaper than any eligible class). Use `--policy A` (scripts) to reproduce.

`scripts/audit_all_optimizations.py` re-derives every recommendation, cost, savings, and conflict flag with independent helpers and must agree with the engine on all records.

### Pricing freshness

Rates come from a committed snapshot. Check freshness with

```bash
python scripts/refresh_pricing.py          # dry-run diff vs the live Price List API
python scripts/refresh_pricing.py --write  # persist when every refreshable line matched
```

The dry run scans every AmazonS3 product for the target region, prints a per-field diff, and refuses `--write` if an expected refreshable line is missing. Two values are known to be absent from the API (Deep Archive's storage rate and restore-request rate) and always keep their snapshot values. `optimization/costs.py` emits a `UserWarning` when the snapshot's `pricing_checked` date is older than the `pricing_max_age_days` budget (90 days).

## Local Development / Testing

```bash
python -m pip install pytest boto3   # dev tooling
python -m pytest tests/ -v           # run all tests
python scripts/generate_synthetic_data.py
```

The document/aggregator Lambda modules require the `DOCUMENTS_TABLE`, `ACCESS_HISTORY_TABLE`, `AGGREGATES_TABLE`, `DOCUMENTS_BUCKET` environment variables (set automatically in Lambda); the decisions Lambda additionally needs `DECISIONS_TABLE`. The test harness sets all of these itself — `python -m pytest tests/` needs no AWS credentials.

## Cleanup

```bash
sam delete --stack-name intelligent-storage-cost-optimizer
```

(Deletes the whole stack including the generated bucket and tables. The SAM-managed deployment bucket persists and can be emptied/deleted separately.)

## Project Status

Implemented and live in the owner's AWS account: the full analysis → **approval → execution → verification → realized-savings ledger** loop (the decisions Lambda runs the real engine, byte-pinned), **model-vs-bill reconciliation over Cost Explorer**, **S3 inventory + access-log ingestion of existing buckets** (with an onboarding wizard), **on-call shift scheduling with two-way PagerDuty override sync and alarm paging** (Events v2, MessageId-idempotent, alarm-ARN dedup so OK auto-resolves), Cognito sign-in on every API route, log retention + error alarms into an SNS topic, a least-privilege deployment preflight, and CI (backend + frontend + template hardening). Test suites: 244 backend, 47 template/preflight, 77 frontend.

Deliberate single-tenant scope (roadmap by design): one AWS account & S3 storage classes per deployment, ap-south-1 pricing snapshot (refresh per region with `scripts/refresh_pricing.py`), multi-account roll-up, additional storage services (EBS/EFS/snapshots), other clouds, SSO/SAML federation, S3 Batch/Step Functions fan-out for million-object migrations, and Trusted Advisor check-surface enrichment for Business+-support customers.