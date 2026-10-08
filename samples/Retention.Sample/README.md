# EF retention compatibility sample

This executable reproduces the **sanitized relationship contract supplied for the POC**, then exercises a proposed parent-aligned composite-key variant through EF Core. It is a small integration fixture, not the work application or a production migration tool.

Run from the repository root with .NET SDK 10, Python 3, Docker, and local `postgres:16` / `postgres:17` images:

```sh
python3 scripts/run-ef-sample.py --image postgres:16
python3 scripts/run-ef-sample.py --image postgres:17
```

The wrapper restores locked NuGet dependencies, builds, starts a fresh PostgreSQL container with an ephemeral **127.0.0.1-only** port and a generated password, runs the sample, and removes that exact container in `finally`. It uses a 1 GiB container limit and 512 MiB temporary database volume. It never connects to AWS. Images are not pulled automatically. A run has a five-minute application deadline. Evidence and diagnostic logs go to ignored `.lab/retention-ef-*/`; failed runs remain failures. The published combined result is `evidence/ef-sample.json`.

The application additionally requires a loopback connection, database name `retention_sample`, and absent `baseline`/`partitioned` schemas. Do not point it at an existing database. It creates schemas and synthetic rows; only the wrapper owns container cleanup. No source attachments or real application/customer data are copied into the project.

## What the sample preserves

| Supplied fact | Sample representation |
| --- | --- |
| Current parent/queue/push use ID-only identity keys | `BaselineContext` and explicitly created baseline DDL |
| Queue maps `HasOne.WithOne`, but the DB has no matching unique constraint | Both EF models retain `WithOne`; DDL deliberately has a non-unique FK index; duplicate-row probe demonstrates the mismatch |
| Queue and push cascade from the parent | Database `ON DELETE CASCADE`, tested through EF bulk parent deletion |
| Parent and queue reference a template with cascade deletion | Both paths included; template deletion tested |
| History has a queue-ID index and no FK | Separate history entity without navigation/FK; deletion leaves history until explicit cleanup |
| Queue delivery time can change | Reschedule across days; composite variant remains in the same parent-aligned partition |
| Parent time was not database-enforced immutable | Composite variant adds explicit rejecting parent/child triggers; this is a proposed change, tested separately |
| Composite partitioning requires parent timestamps in child FKs | Copy rehearsal joins each child to its parent to populate the extra key component |
| Global bare-ID uniqueness is lost with composite partition keys | Controlled override demonstrates duplicate bare IDs across timestamps |

This is handwritten, minimal DDL and EF mapping derived from the supplied review. It does not claim full production DDL, all indexes, or exact application query text. `EnsureCreated` is deliberately not used: it would silently add queue uniqueness from the ORM model and conceal the supplied discrepancy. A pair-level uniqueness constraint could enforce one queue per composite parent, but is not added here.

## Proof sequence

1. Create the current schema and persist a graph through EF, including database-generated identities. Load the relationships back through EF.
2. Copy the small baseline into the composite model in a transaction holding source access-exclusive locks. Compare every original column in both directions with `EXCEPT ALL`, validate the new FKs via actual inserts, and advance all identity sequences. Insert a new graph/history/template to verify sequence high-water marks, then roll it back. This is a quiescent copy/backfill rehearsal, not an in-place conversion or rename swap.
3. Exercise rescheduling, parent/template cascades, duplicate queue acceptance, soft-history survival, and rollback; compare all fixture rows afterward. Test parent and child retention-key immutability and the composite identity limitation. Committed duplicate queue IDs exercise full-key EF rescheduling and fail-closed history archival, including a collision introduced after the initial snapshot.
4. Execute 64 fenced EF probes: eight write shapes × four plan settings × cold/warm connections. Each has an unfenced control; warm cases execute eight controls on the same physical pooled connection. Record server prepared-statement SQL and custom/generic counters before and after the fenced attempt. Unexpected SQL errors, row counts, or prescribed outcomes fail the run. Every probe rolls back, including successful writes.
5. In one archive transaction, lock expired leaf partitions and the soft-history table, copy expired rows to local archive tables, then commit ten live EF graphs and read them through an independent connection while that transaction remains open. Reject ambiguous bare queue IDs before snapshotting and recheck under parent access-exclusive locks immediately before retirement; verify archive/source equality, retire child-first, explicitly clean expired soft history, and commit. Fresh EF reads verify surviving counts and acknowledged graph fields.

Local archive tables are a stand-in for the application integration step. **This sample does not export to S3 or measure archive durability.** Prior AWS receipts remain separate evidence. The template table is not included in these archive tables, matching the known archive join gap rather than silently resolving that business decision.

## Plan modes and limits

`Max Auto Prepare=0` tests Npgsql's default disabled automatic preparation with PostgreSQL `plan_cache_mode=auto`. Other runs enable automatic preparation with a threshold of two uses and force custom, force generic, or leave PostgreSQL in automatic mode. Separate pools prevent one candidate from warming another. The warm cases close/reopen EF contexts but verify that the physical connection was reused. Forced generic planning can affect an unnamed extended-protocol execution even before named auto-preparation; cold does not mean PostgreSQL ignores `plan_cache_mode`.

The complete observed matrix is asserted, including qualified automatic-mode admission. If another engine/data distribution chooses a different automatic plan, the fixture fails and records that incompatibility; this is not a guarantee about arbitrary workloads. Parameterless SQL can report generic counters even in force-custom mode; counter validation accounts for that. The SQL capture records command templates, not secrets or real values.

The pinned sample uses Npgsql EF provider 10.0.2, Npgsql 10.0.3, and EF Core 10.0.4 on .NET 10. These are **sample choices**, not a claim about work's deployed versions. Npgsql documents [automatic preparation and persistence across pooled connections](https://www.npgsql.org/doc/prepare.html). Dependency versions are locked in `packages.lock.json` and runtime versions are recorded in receipts.

Two daily partitions and a tiny graph prove bounded behavior only. Multiple simultaneous writers, production query/index coverage, 2M-row sizing, WAL/vacuum, failover, ambiguous commit recovery, RDS Proxy, Aurora wire-protocol behavior, payload/identity splitting, and full migration cutover remain separate experiments. The concurrent fixture has one sequential writer and reader overlapping an open archive transaction; it does not claim sustained throughput or unrestricted concurrent mutation. Soft-history SHARE locking deliberately blocks history writes during this fixture.

The wrapper validates the application receipt schema and every successful matrix outcome. Cleanup errors are recorded separately from execution errors and still produce a failed envelope. Unit tests inject malformed receipts, reverse all 64 outcomes individually, and simulate cleanup exceptions/nonzero exits.
