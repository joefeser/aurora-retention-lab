# Phase 6: larger synthetic case and decision report

The POC supports a conditional next step, not a production deployment decision: **evaluate parent-aligned partitioning only if the application can adopt composite keys and partition-qualified write paths.** Keep indexed batched deletion as the comparison baseline. The lab has not shown that a transparent change to the existing ID-only design can deliver safe online archival.

## Larger bounded case

```sh
python3 -m unittest discover -s tests -v
python3 scripts/run-retirement.py --rows 4096 --push-per-parent 8
```

The retirement runner now admits 1,024 or 4,096 expired parents and two or eight push rows per parent. Defaults preserve phase 3. The archive gate validates the selected counts, including fanout, before destructive operations. Export, import, and exact comparison now use chunks of at most 512 parent IDs (16 MiB of parent body per chunk); each restored table must also have the expected total count. This chunked path also applies to the default fixture, so historical phase-3 archive timings describe the earlier whole-table path. A negative test rejects a small-fixture receipt or incorrect push count for the larger fixture.

The larger case has 4,096 expired parents, 4,096 queues, 32,768 push rows, and 4,096 soft-history rows per arm: 45,056 expired rows and 160 MiB of parent/queue body payload. Each arm also has 32 live parents and their corresponding relationships. Both designs run twice in reversed order, on the existing maximum-1-ACU cluster. This comparison retains phase 3's quiescent protocol: parent access-exclusive locks cover export, verification, and retirement. It does not combine the larger load with phase 4's concurrent writers. All data remains deterministic and synthetic.

This increases row count and fanout together. It is a combined stress case, not an isolated causal measurement of either factor. It does not reproduce work data distributions, skew, real HTML, query mixes, or contention. No production migration duration can be inferred from it.

### Observed results

The first whole-table attempt hit `StatementTimeoutException` during the first relation's archive/restore path under the unchanged 35-second statement limit. The original receipt did not distinguish which of export, import, or comparison timed out, so the precise operation is unknown. No retirement was reached. Fresh requests confirmed all seeded relation counts remained and transactional restore/timing tables were absent after rollback. The failed result is retained in `evidence/retirement-stress-timeout.json`.

The retry uses bounded chunks and records the current export/import/comparison stage before each request. Neither the capacity ceiling nor the timeout was raised. All four arms of the chunked retry passed on October 8, 2026. All 128 objects passed SSE-KMS and exact restored-content checks. Fresh post-commit requests confirmed keeper contents and surviving cascade FKs; all expired partition relations were absent in the partition arms. Six Python unit tests passed, including rejection of incorrect fixture counts.

| Trial | Design | Retirement, server | Archive and verification | Parent lock acquisition through commit |
| --- | --- | ---: | ---: | ---: |
| 1 | delete | 9.620032 s | 94.419 s | 108.286 s |
| 1 | partition | 0.055764 s | 98.325 s | 102.701 s |
| 2 | partition | 0.053504 s | 94.443 s | 98.472 s |
| 2 | delete | 10.867606 s | 96.834 s | 111.819 s |

The run completed in 597.704 seconds including setup. Evidence: `evidence/retirement-stress.json`. DELETE retirement took 9.620–10.868 seconds; partition retirement took 0.054–0.056 seconds in these two samples each. Archive/verification took 94–98 seconds regardless of retirement method and dominates the partition path. This demonstrates a retirement-time advantage for this fixture only, not a production speedup or backlog ETA. The table also shows why timing only the DROP would conceal the much longer blocking interval in this deliberately quiescent protocol.

## Decision matrix

| Choice | Evidence from this lab | Remaining gate |
| --- | --- | --- |
| Parent-aligned partitions with composite FKs | Exact S3 restores, coordinated retirement, mutable delivery time separated from retention identity, live insert/read progress | Work approval for key changes, all relationship/retention semantics, actual EF and prepared query shapes, production recovery design |
| Preserve current ID-only design and batch DELETE | Indexed cascade baseline preserves relationships with explicit history cleanup | Representative batch sizes, vacuum/WAL/replication impact, backlog duration, operational load and archive fencing |
| Copy keepers and swap tables | Local cooperative writer shutdown preserved acknowledgements; naive live copy missed committed writes | Production freeze window or a separately designed change-capture/catch-up protocol, FK and sequence handling, real rollback plan |
| Separate archive database or split payload storage | Not benchmarked | Retention/restore use cases, complete schema/application change assessment and operating cost |

Partitioning is not an additional key that can be bolted onto the existing contract unchanged. The tested design carries the parent's retention timestamp into primary and foreign keys. A composite key does not enforce global uniqueness of bare ID. Existing EF identity assumptions, joins, one-to-one uniqueness, all incoming FKs, and updates need explicit review. Soft history requires a policy of its own; the synthetic parent-aligned policy is not a business decision.

Phase 5 identified a concrete compatibility problem: ID-only live UPDATE and cascading DELETE blocked under expired-leaf fencing, while tested forms with a constant partition predicate were admitted. Phase 4 therefore proves concurrent **inserts and reads**, not unrestricted online archival. Prepared/generic plans, pools, triggers, bulk operations, and all real writers still require tests.

## Evidence established across the POC

- Local PostgreSQL 16.15 and 17.11 compatibility checks exposed the partition-key and identity differences. Upgrading alone does not remove the key redesign.
- Aurora 17.9 exported and restored synthetic data exactly, including 128 MiB and 1 GiB chunked baselines, with SSE-KMS checks.
- Coordinated child-first retirement preserved active relationships. Restrictive DDL is essential; DROP CASCADE is not a substitute for row-level cascades.
- Synthetic concurrent writes remained intact in the cooperative local cutover and the Aurora insert/read archive fixture.
- Expired-leaf write fencing blocked mutation attempts. Releasing the fence allowed the archive to become stale.
- Confirmed rollback, reader contention, and an injected partial-DDL exception were recoverable in bounded fixtures. Failover, lost acknowledgements, and automatic crash recovery remain unproven.

Evidence files and each phase's method distinguish these claims. They do not establish seven-year retention compliance, immutable backups, restore SLAs, RDS Proxy behavior, or production readiness. Aurora 16 performance was not measured.

## Recommended next work with the work team

Use [the sanitized validation request](work-validation-request.md) to obtain schema/query facts and dataset profiles. Then run an application compatibility and representative migration rehearsal. Do not spend further lab capacity producing increasingly large arbitrary synthetic tables in place of those inputs.

A production archive controller would need a durable attempt/manifest state model, independent verification, fenced ownership, retry and commit reconciliation, and explicit restore/cutover/rollback procedures. Those are implementation projects beyond these disposable runners. A retained old table is not a lossless rollback once writes resume elsewhere.

## Review stack and teardown

Review and merge the recovery PR first, then the stress/decision PR based on its branch. After the first merge, verify the second PR's base and diff against main, especially after a squash merge; do not blindly merge a diff that includes the predecessor again. No PR is automatically merged by this work.

The teardown reminder is October 9, 2026 at **11 AM Central**. It is a reminder, not automatic deletion. Use `python3 scripts/aws-lab.py delete`, then verify `DELETE_COMPLETE` and remaining inventory. The original USD 50 budget remains in force; a 1 ACU ceiling does not cap total spend, and billing telemetry can lag. This phase adds no infrastructure. All synthetic tables, restores, and S3 objects remain until teardown.
