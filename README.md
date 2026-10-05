# Intelligent Cloud Storage Cost Optimizer

An AWS-based system that analyzes cloud object access patterns and recommends cost-effective storage classes while considering storage, request, retrieval, and transition costs.

## Problem

Organizations often keep large collections of documents in frequently accessed storage even when many objects become rarely accessed over time. This can result in unnecessary storage costs.

This project explores an access-aware approach to cloud storage optimization, demonstrated with a **synthetic legal-document storage use case** (no real personal data).

## Architecture

```
                     ┌────────────────┐
  Client ───────────►│ API Gateway    │  HTTP API ($default stage)
                     │ (DocumentsHttp │
                     │      Api)      │
                     └───────┬────────┘
              /documents*    │    /aggregates/run
                             ▼
              ┌───────────────────────────┐
              │      Lambda python3.13    │
              │  DocumentsFunction        │  AggregatesFunction
              │  create/list/get/download │  aggregate 30-day features
              └──────┬───────────┬────────┘
                     │           │
     ┌───────────────▼───┐   ┌───▼────────────────────┐
     │  DynamoDB         │   │ DynamoDB                │
     │  DocumentsTable   │   │ AccessHistoryTable      │
     │  (metadata)       │   │ (raw access events)     │
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
```

### AWS resources (all created by CloudFormation/SAM)

| Resource | Type | Notes |
|---|---|---|
| `DocumentsBucket` | S3 Bucket | AES256 encryption, public access blocked |
| `DocumentsTable` | DynamoDB | PROVISIONED 1 RCU/1 WCU, PK `document_id` |
| `AccessHistoryTable` | DynamoDB | PAY_PER_REQUEST, PK `document_id`, SK `access_timestamp` |
| `DocumentAggregatesTable` | DynamoDB | PAY_PER_REQUEST, PK `document_id` |
| `DocumentsFunction` | Lambda | Python 3.13, documents API |
| `AggregatesFunction` | Lambda | Python 3.13, aggregation API |
| `DocumentsHttpApi` | HTTP API | `$default` stage |
| `deployment-policy.json` | (file, not resource) | Least-privilege policy template for the deployment identity; admin-reviewed, placeholder-based |

**All resource names (bucket, tables, functions, API URL) are generated dynamically by CloudFormation in whatever account/region you deploy to. Never hard-code them**; read them from `sam list resources` / CloudFormation outputs / the stack description.

## Repository Structure

```
backend/lambdas/documents/   Document API Lambda
backend/lambdas/aggregates/  30-day aggregation Lambda
optimization/                Pure-Python optimizer (models, costs,
                             constraints, baseline, adapters)
data/synthetic/              Deterministic synthetic test data
scripts/                     generate/load/validate/audit/refresh tools
infrastructure/template.yaml AWS SAM template
tests/                       pytest suite (65 tests)
experiments/ research space ·  frontend/ dashboard space ·  docs/ architecture docs
```

## API Endpoints (live stack)

| Method | Path | Behavior |
|---|---|---|
| POST | `/documents` | Create metadata + presigned S3 upload URL (needs `file_name`, `file_size`, `content_type`) |
| GET | `/documents` | List all documents |
| GET | `/documents/{document_id}` | Get one document |
| GET | `/documents/{document_id}/download` | Record a DOWNLOAD access event + presigned download URL |
| GET | `/documents/{document_id}/access-stats` | All-time count, last access, 30-day count |
| POST | `/aggregates/run` | Aggregate all documents into `DocumentAggregatesTable` |

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
| `DOCUMENTS_BUCKET` | DocumentsFunction | Generated S3 bucket name |
| `DOCUMENTS_TABLE` | Both | Generated documents metadata table |
| `ACCESS_HISTORY_TABLE` | Both | Generated access-history table |
| `AGGREGATES_TABLE` | AggregatesFunction | Generated aggregates table |

## Synthetic Data

The synthetic dataset is deterministic (fixed random seeds, reference timestamp `2026-09-12T00:00:00Z`) and mimics a law firm: 500 documents with file names/content types, practice-area metadata, mixed storage classes, document states (ACTIVE/CLOSED/ARCHIVED/null), and mixed 30-day access patterns (0 to 300 downloads per document, ~15,200 raw events total).

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

`optimization/` projects each document's annual cost per candidate storage class from the snapshot in `optimization/pricing.json` (Mumbai/ap-south-1 by default) and recommends the cheapest. The cost model covers storage, retrieval fees, GET request charges, and lifecycle transition requests — **per 1,000 objects** — and includes the billing details that matter for small or young legal files:

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

The document/aggregator Lambda modules require the `DOCUMENTS_TABLE`, `ACCESS_HISTORY_TABLE`, `AGGREGATES_TABLE`, `DOCUMENTS_BUCKET` environment variables (set automatically in Lambda).

## Cleanup

```bash
sam delete --stack-name intelligent-storage-cost-optimizer
```

(Deletes the whole stack including the generated bucket and tables. The SAM-managed deployment bucket persists and can be emptied/deleted separately.)

## Project Status

Milestone 1 (live API + tables) and the optimization engine are implemented; the aggregation bridge (`AggregatesFunction` → `DocumentAggregatesTable`) is live. Planned next: scheduled optimization, controlled automatic transitions, and a dashboard frontend.