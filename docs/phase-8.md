# Phase 8: EF application compatibility sample

The work-side sanitized review already supplied the relationship and workload design inputs used by this lab. This phase turns those inputs into a runnable [.NET/EF sample](../samples/Retention.Sample/README.md). The remaining work is validation coverage and business decisions; it is not a blanket request to supply the schema again.

The sample compares the current ID-only relationship model with parent-aligned composite keys, preserving the supplied ORM/database uniqueness mismatch, push cascades, soft history, template cascades, and mutable delivery scheduling. It deliberately adds a database immutability trigger only to the proposed composite model. Exact work SDK/provider versions were not established by the supplied review, so the sample pins and records its own versions.

## Run and observed result

```sh
python3 scripts/run-ef-sample.py --image postgres:16
python3 scripts/run-ef-sample.py --image postgres:17
```

Both PostgreSQL 16.15 and 17.11 passed with EF Core 10.0.4, Npgsql 10.0.3, and Npgsql EF provider 10.0.2 on .NET 10. Each engine ran 64 fenced EF write probes with unfenced controls, including eight warm-up executions per warmed candidate. Published evidence includes actual EF SQL, prepared-plan counters, engine/image identities, and temporary-container cleanup results: [ef-sample.json](../evidence/ef-sample.json).

The observed matrix was identical across these two engines and cold/warm cases:

| EF write shape under expired-leaf SHARE locks | Auto-prepare disabled, automatic planning | Force custom | Force generic | Auto-prepare enabled, automatic planning |
| --- | --- | --- | --- | --- |
| Parent bulk UPDATE, ID only | Blocked | Blocked | Blocked | Blocked |
| Parent bulk UPDATE, ID + timestamp | Admitted | Admitted | Blocked | Admitted |
| Tracked parent `SaveChanges`, composite identity | Admitted | Admitted | Blocked | Admitted |
| Queue reschedule, ID + parent timestamp | Admitted | Admitted | Blocked | Admitted |
| Parent bulk DELETE, ID only | Blocked | Blocked | Blocked | Blocked |
| Parent bulk DELETE, ID + timestamp | Admitted | Admitted | Blocked | Admitted |
| Template DELETE with cross-partition cascades | Blocked | Blocked | Blocked | Blocked |
| Expired parent UPDATE, ID + timestamp | Blocked | Blocked | Blocked | Blocked |

“Blocked” means SQLSTATE `55P03`; “admitted” means the mutation succeeded and was deliberately rolled back. Every probe/control restored the fixture's full contents. Warm cases verified reuse of the same physical pooled connection and actual prepared DML counters. The automatic planner retained custom plans for qualified parameterized writes in this tiny fixture; it switched the ID-only probes to generic plans after five custom uses. This observation is not a guarantee for other data distributions or partition counts. Parameterless template deletion uses generic counters even under force-custom mode.

Forced generic mode blocked qualified writes even on their cold, unnamed extended-protocol execution before automatic preparation. Named prepared-statement counters are not available for that cold execution; the outcome and configured setting are distinct evidence from the warmed counters.

## Additional application evidence

- EF inserts and reloads parent/queue/push graphs with generated identity values and matching FKs.
- A bounded source-locked copy/backfill into the composite schema preserves every original column across all five tables. New identities exceed the copied rows. This does not perform an online rename swap or measure migration speed.
- Rescheduling delivery into another day keeps the queue in its original parent-aligned partition.
- Parent/template deletion cascades as described; soft history survives without explicit cleanup.
- Both schemas permit duplicate queue rows despite `WithOne`. No extra unique constraint was silently introduced.
- The proposed parent immutability trigger rejects timestamp changes. Composite PKs still permit duplicate bare IDs across timestamps.
- Ten committed EF graphs and independent reads progress while a local archive transaction holds the expired leaves. Local archived rows still match before child-first retirement; fresh EF reads after commit verify live graph fields and counts. Soft history is explicitly fenced and cleaned.

## Decision and remaining coverage

Composite-key EF mapping is feasible in this bounded sample, but it does not make all application writes compatible with archival. ID-only writes, template cascades, and generic-plan qualified writes remain concrete blockers under the tested fence. Keeping a parent timestamp in the key helps tracked EF writes, but it does not establish global ID uniqueness or decide the history/template retention policy.

The sample uses local archive tables, not S3; prior AWS export/restore receipts remain a separate evidence layer. It has two daily partitions, one sequential live writer, and an independent reader overlapping the archive transaction. It does not prove work's exact EF/provider release, full query/index inventory, high concurrency, Aurora/proxy wire behavior, workload sizing, a production migration, or automatic recovery. The source-locked copy, SQL admission, and archive retirement checks must not be combined into a claim that an online production migration is ready.

No AWS infrastructure was added or changed. The October 9, 11 AM Central teardown reminder and original USD 50 lab budget are unchanged.
