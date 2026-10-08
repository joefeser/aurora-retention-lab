# Phase 2: bounded load and concurrent migration rehearsal

This phase starts from merged PR #1. It measures a larger S3 round trip and exercises a cooperating writer during migration. It does not choose a production architecture or authorize applying the scripts to a work database.

## Observed results, October 8, 2026

Both local PostgreSQL 16.15 and 17.11 passed all seven concurrency assertions. Each recorded eight acknowledged writes before cutover and four after resume; all appeared with matching payloads and related records in the new active tables. The measured quiesce/copy interval was about 0.068 seconds for this tiny fixture. That number must not be used as a production freeze estimate.

Aurora PostgreSQL 17.9 completed both bounded runs at a maximum of 1 ACU:

| Logical body payload | Rows | Generation | Export calls | Import calls | Entire run | Logical export rate |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 128 MiB | 4,096 | 20.454 s | 11.935 s | 13.380 s | 57.168 s | 10.725 MiB/s |
| 1 GiB | 32,768 | 136.605 s | 96.795 s | 104.729 s | 405.594 s | 10.579 MiB/s |

All 72 chunks across both runs passed full payload comparison and SSE-KMS checks. Rates include CLI/API overhead and use logical body bytes, not CSV bytes or billed storage. PostgreSQL reported source relation sizes of 140,435,456 and 1,125,089,280 bytes respectively, including indexes and TOAST. There was one measured run per size; these are baseline samples, not percentiles or saturation limits. Evidence: `evidence/concurrency.json` and `evidence/scale.json`.

## Concurrent copy-keepers rehearsal

```sh
python3 scripts/run-concurrency.py --image postgres:17
python3 scripts/run-concurrency.py --image postgres:16
```

The runner creates its own network-isolated temporary Docker database and removes it on exit. A writer commits parent, queue, push, soft-history, and acknowledgement-ledger records in one transaction. Every modeled operation opens and closes its own database connection.

First the test copies keepers while writes continue and proves the copy misses subsequently committed records. It then requests writer shutdown, waits for acknowledgement and completion of pending operations, locks all modeled tables, copies the retained relationship set, rebuilds FKs, advances the new identity sequence, and swaps names. A new writer connection resumes afterward.

Acceptance requires matching client acknowledgements to the committed ledger, preserving every acknowledged parent payload and related record, preserving initial keepers exactly, excluding expired records, and binding the new queue FK to the active parent. A final negative test proves the retained old copy is stale after writes resume.

This is **cooperative writer quiescence**, not an online migration algorithm. Every real writer, pooled/prepared connection, background job, and external integration must be addressed before using a comparable production cutover. The measured freeze on a few hundred rows is not a production downtime estimate. The lab uses a synthetic `keep` flag; it does not decide retention semantics. It retains the old tables until the temporary container is removed; archive-before-delete compliance is not exercised here.

## Bounded Aurora load

Run against the dedicated stack after the phase-1 AWS experiment has installed the extensions:

```sh
python3 scripts/run-scale.py --mib 128
# Only after the smaller run passes:
python3 scripts/run-scale.py --mib 1024
```

The only admitted sizes are 128 MiB and 1 GiB of logical text payload. Each row has a 32 KiB body constructed from 1,024 distinct deterministic MD5 blocks. This avoids repeating a short string but is still artificial hex text, not measured production HTML. The receipt records actual table/index/TOAST storage separately from logical bytes.

The runner checks the live cluster's capacity ceiling is at most 1 ACU. It loads 512 rows at a time and exports/imports 512-row chunks (16 MiB of body data per object). Every export must report the expected row/file count and have SSE-KMS metadata. Every restored chunk must match the original payloads and row count; primary keys reject duplicates. The final restored total must match the requested load.

The 30-minute wall-clock guard is checked before each database call. It is not an AWS billing cutoff or cancellation of an already-running statement. Errors stop the run with `passed: false`; source, partial restores, and S3 objects remain available for inspection. Retries create new schemas and object prefixes. No scale-run source is retired, and no storage is automatically deleted by the benchmark.

Export/import durations include AWS CLI/API overhead. Overall time also includes synthetic generation, object metadata checks, and content validation. This measures sequential small-object exports, not AWS's automatic splitting of a single multi-gigabyte export. There are no concurrent application writers during the AWS load test, and the benchmark uses one non-partitioned payload table. It does not compare full production DELETE/cascade throughput with partition retirement or establish a 100-million-row migration duration.

## Resources and evidence

The existing stack is reused; no second Aurora instance, higher capacity, or new network infrastructure is required. The original USD 50 total budget and October 9 teardown plan remain in effect. Billing telemetry may lag. Larger fixtures retain roughly two copies of the payload in Aurora plus one S3 export until the dedicated stack is deleted.

Each run writes an ignored `.lab/<run>/evidence.json`. Public evidence under `evidence/` must remain synthetic and omit account IDs, secret ARNs, bucket names, and private endpoints. Use `python3 scripts/aws-lab.py delete` for the dedicated stack's teardown and verify `DELETE_COMPLETE`.
