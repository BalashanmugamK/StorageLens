# The approval → execution → verification loop

How a recommendation becomes a real, verified storage-class transition
— and why every step is shaped the way it is.

## The problem the loop solves

The offline engine (`optimization/`) ends at recommendations. A
recommendation nobody can act on is a report, not a product: the gap
between *analysis* and *execution* is where a cost optimizer usually
dies. The decisions Lambda (`backend/lambdas/decisions/app.py`) plus
the `DecisionsTable` close that gap, with the approval gate in the
middle because an automated S3 migration is a real, sometimes
irreversible, operational change.

```
POST /decisions          proposed ──┐
   engine re-run                    │ approve          reject
                                    ▼                      ▼
POST .../approve               approved              rejected
   human gate                        │
                                    ▼ execute (S3 CopyObject)
POST .../execute               verified ──► realized_savings ──► /ledger
   copy + HEAD-verify             └─► failed (verification_error)
```

## Decision records

`DecisionsTable` stores the decision, not just the outcome:

| Attribute | Why it is recorded |
|---|---|
| `from_class` / `to_class` | The migration being proposed |
| `predicted_from_cost` / `predicted_to_cost` / `predicted_savings` | The engine's forecast at proposal time |
| `features_as_of` | Aggregation timestamp the forecast consumed — reviewer provenance |
| `engine_input` | The access features (count, frequency, recency) behind it |
| `policy` / `policy_conflict` | Which policy ran and whether it flagged a compliance conflict |
| `decision_state` + stamps | `created_at` / `approved_at` / `executed_at` / `verified_at` / `rejected_at` |
| `realized_savings` | Set only on HEAD verification, never on approval |
| `verification_error` | Failed transitions carry their reason |

GSIs: `state-index` (`decision_state` HASH, `created_at` RANGE)
partitions the approval queue and the ledger; `document-index`
(`document_id` HASH, `created_at` RANGE) backs the one-open-decision
guard.

## Invariants (each pinned by a test)

1. **Approve before execute.** `execute_decision` refuses every state
   except `approved` (`tests/test_decision_lambda.py::test_execute_refuses_a_non_approved_decision`).
2. **Verification before realization.** After `copy_object`, a HEAD
   must report the target class (Intelligent-Tiering may report
   `STANDARD`); on match and only on match the state becomes
   `verified` and `realized_savings` is written. A mismatch or S3
   failure writes `failed` + `verification_error` and returns 500 —
   never a silently claimed saving.
3. **One open decision per document.** Proposing while a
   `proposed`/`approved` decision exists for the same document is a
   409, so concurrent approvals can never migrate an object twice.
   (`DecisionsFunction` policies grant Query on `${DecisionsTable.Arn}/index/*`
   for exactly this check.)
4. **Execution is POST-only.** `GET /decisions/{id}/execute` returns
   405 before touching S3 — a browser prefetch or link crawler cannot
   fire a storage transition.
5. **Decimal discipline.** DynamoDB rejects floats; every write goes
   through `to_dynamo` (float → `Decimal(str(v))`, recursive), and the
   response serializer converts back for the JSON wire. Nested engine
   figures (including the `engine_input` subdocument) survive
   `put_item`.
6. **The engine is the engine.** Lambda deploys one directory, so
   `backend/lambdas/decisions/engine/` is a byte-identical copy of
   `optimization/`; `tests/test_engine_parity.py` fails the suite if
   either side changes alone, and runs the copy standalone (`python -I`)
   to prove it imports without the `optimization/` package. Policy B
   (cost-safe) is fixed on the execute path — the live system runs the
   same policy the experiments ran.

## Realized-savings honesty

The ledger's `basis` string is written by the backend and echoed by
the frontend verbatim:

> predicted annual savings carried by decisions whose S3 copy was
> verified via HEAD - not invoice data

Real invoice-grade savings need the AWS Cost Explorer / CUR feedback
loop (per-transition line items), which is future work. Until then the
ledger labels itself exactly: a HEAD-verified transition proves the
*state* change, and the savings number carried is the engine's
predicted annual figure, refreshed only when someone re-aggregates and
re-proposes. The frontend's Ledger tab repeats this basis and shows
the features freshness of its entries rather than dressing the data up
as accounting.