# Phase 4: archive while live inserts and reads continue

This phase tests a narrower locking strategy on the existing Aurora 17.9 lab. Phase 3 held access-exclusive locks across archive and restore verification. Here, a single transaction holds SHARE locks on the four expired leaf partitions during archival and upgrades to access-exclusive locks on the parent tables only for coordinated retirement.

```sh
python3 -m unittest discover -s tests -v
python3 scripts/run-live-archive.py
```

## Workload and acceptance

Each fresh synthetic schema starts with 1,024 expired parents, one queue and soft-history row per parent, and two push rows per parent. Parent bodies are 32 KiB and queue bodies are 8 KiB, generated with the same deterministic data pattern as phase 3. Queue and push have indexed composite cascade FKs; history remains a soft relationship.

A writer repeatedly inserts a live parent, queue, push, history, and acknowledgement ledger entry in one auto-committed database statement through the parent tables. A reader repeatedly scans the parent and checks that a single statement snapshot contains all ledger relationships. Both use independent Data API calls. Every acknowledged write ID and every operation's client latency and starting/ending phase are captured.

The archive transaction takes SHARE locks on all four expired leaves. Seventeen independent-session probes must raise SQLSTATE `55P03` through a 250 ms lock timeout: UPDATE, DELETE, direct-leaf INSERT, and parent-routed INSERT for each relation, plus an attempted live-to-expired parent timestamp change. The probe catches only that timeout; any other error or admitted mutation fails the run. These statements test lock admission, including attempts that would fail other constraints if they reached execution.

All four expired relations are exported, checked for SSE-KMS, restored, and compared column-for-column with `EXCEPT ALL` in both directions under the same held locks. The shared retirement guard requires the complete verified set before proceeding. At least three writes and three reads must start and finish entirely inside each of the before-archive, archive, and after-retirement phases.

Retirement upgrades the parent locks, drops expired history/push/queue leaves, then detaches and drops the expired parent leaf using restrictive DDL. It commits before the after-retirement phase. The reader and writer remain running during retirement and may briefly wait. They are then stopped cooperatively, with in-flight operations allowed to finish. Fresh requests must prove:

- Client acknowledgements exactly match the committed ledger.
- Every live row, payload, timestamp, and relationship matches the writer's expected values.
- All expired leaf relations are absent.
- Both cascade FKs remain validated against the active parent.
- No reader or writer reported an error.

## Observations

Two runs passed on Aurora PostgreSQL 17.9 on October 8, 2026. All eight archives passed SSE-KMS and exact content checks. Both runs rejected all 17 mutation probes. All 104 acknowledged writer transactions survived with matching payloads and relationships, and all 106 reader requests returned consistent results. No worker errors were recorded. Five existing Python unit tests for encryption and the retirement guard also passed.

| Run | Total writes | Total reads | Writes / reads entirely within archival | Archive, probes and verification | Parent lock acquisition through commit |
| --- | ---: | ---: | ---: | ---: | ---: |
| 1 | 53 | 54 | 42 / 43 | 23.571 s | 1.352 s |
| 2 | 51 | 52 | 40 / 41 | 23.462 s | 1.322 s |

Maximum observed client write latency was 1.309 s in run 1 and 1.281 s in run 2; maximum read latency was 1.225 s and 1.281 s respectively. These include CLI/API overhead and are sample maxima, not percentile estimates. The roughly 1.3-second retirement intervals include parent-lock acquisition, DDL, and commit calls; they are not server-only lock-hold measurements. The roughly 23.5-second archive intervals include the deliberately blocked probes and are not directly comparable with phase 3's archive-only timings.

Fresh independent requests checked all acknowledged relationships and payloads after each run. `evidence/live-archive.json` contains sanitized operation receipts, phase labels, and checks. The first run preceded the addition of the runner's final all-field relationship assertion; the independent post-run assertion passed for both, and the second run exercised the final runner assertion as well.

## What this establishes and what remains open

The evidence covers one sequential append-only writer, one reader, a small fixed backlog, and a single Aurora 17.9 instance capped at 1 ACU. API/CLI overhead dominates the operation rate; this is a concurrency correctness experiment, not a throughput or latency SLA benchmark. Two samples cannot establish tail latency or fairness under contention.

This is a transaction-scoped write fence for ordinary database operations, not a persistent immutable/archive state. Releasing or losing the archive transaction before retirement permits expired writes again; exports from that attempt cannot be assumed current. Errors roll back the active archive/retirement transaction when possible. Previously committed seeds and live writes remain, as do non-transactional S3 exports. Inspect an uncertain commit outcome before recovery. Each invocation uses a new schema and object prefix rather than resuming partial work.

The allowed live traffic here is specifically parent-routed INSERT and read-only SELECT. General UPDATE/DELETE statements, FK cascades, prepared/pool connections, ORM behavior, administrative DDL, multiple writers, deadlocks, long readers, lock starvation, and failover are not established. A query targeting live rows may still request locks on expired partitions; the results must not be generalized to every application query. SHARE locks also do not prevent a privileged operator from terminating the archive session or altering the protocol.

The next application-facing step is to inventory actual write/query shapes and test them against this fence, including long transactions and recovery after interruption. Production still needs composite-key application changes, a durable archive manifest and recovery state machine, and representative load tests. No claim is made about Aurora 16; the earlier local version matrix remains separate.

The runner verifies the recorded stack and live capacity ceiling. The archive transaction uses a 2-second lock timeout and 35-second statement timeout; the writer uses a 2-second lock timeout. A 30-minute guard is checked before database requests; it does not cancel running calls or cap billing. No new infrastructure is added. The USD 50 budget and October 9 teardown plan remain in effect. Use `python3 scripts/aws-lab.py delete`, then verify `DELETE_COMPLETE`.
