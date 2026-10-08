# Phase 7: prepared plan admission under the archive fence

A constant partition predicate in literal SQL is not enough evidence for a prepared application query. This local fixture compares forced custom and forced generic plan modes on PostgreSQL 16.15 and 17.11, with the same expired-leaf SHARE fence used in the cloud experiments.

```sh
python3 scripts/run-prepared-plans.py --image postgres:16
python3 scripts/run-prepared-plans.py --image postgres:17
```

Both runs passed 14 unfenced controls, classified 14 fenced attempts, and proved both tables were unchanged afterward. Containers used no network, exposed no ports, had a 1 GiB memory limit and 512 MiB temporary data volume, and were removed successfully. No AWS requests or additional cloud resources were needed. Exact image IDs and receipts are in `evidence/prepared-plans.json`.

## Observed matrix

The outcomes were identical on local 16.15 and 17.11:

| Prepared query, first execution under the fence | Forced custom mode | Forced generic mode |
| --- | --- | --- |
| Live parent UPDATE, ID only | Blocked `55P03` | Blocked `55P03` |
| Live parent UPDATE, ID and timestamp parameter | Admitted | Blocked `55P03` |
| Live queue UPDATE, ID only | Blocked `55P03` | Blocked `55P03` |
| Live queue UPDATE, ID and timestamp parameter | Admitted | Blocked `55P03` |
| Live parent cascading DELETE, ID only | Blocked `55P03` | Blocked `55P03` |
| Live parent cascading DELETE, ID and timestamp parameter | Admitted | Blocked `55P03` |
| Expired parent UPDATE, ID and timestamp parameter | Blocked `55P03` | Blocked `55P03` |

All admitted mutations are deliberately rolled back by an injected subtransaction exception. Unexpected errors fail the fixture. Unfenced controls establish that each statement can execute and that the configured plan mode increments the corresponding `pg_prepared_statements` counter. Fenced attempts capture the configured mode and both counters.

Every blocked attempt in these runs timed out before either cached-plan counter incremented. The result therefore describes **admission under a forced plan mode**, not execution of a completed generic plan. Admitted custom-mode attempts recorded one custom plan. The fixture does not infer a completed plan from a mode setting alone.

## Consequence and boundaries

Work-side validation must include plan-cache behavior, not just whether the SQL text contains a partition timestamp. These local results reinforce the need to test actual ORM/provider behavior before treating archival as online for arbitrary writes. They do not recommend changing `plan_cache_mode` globally or bypassing the expired-data fence.

Each probe uses a new session, PREPARE, and its first attempted execution under the fence. The separate unfenced controls use separate sessions. Reused/warmed plans, automatic custom-to-generic selection, pooling, EF, long transactions, multiple writers, and Aurora prepared-statement behavior remain unproven. The Data API literal-query results from phase 5 are a separate evidence layer. This is a two-row relationship fixture, not a performance test.

Review this follow-on after the recovery and stress/decision PRs. The October 9, 11 AM Central teardown reminder remains unchanged.
