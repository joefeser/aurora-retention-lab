# Observed feasibility findings

## Local proof, October 8, 2026

The fixture in `sql/feasibility.sql` passed all 19 assertions on PostgreSQL 16.15 and PostgreSQL 17.11 (Linux ARM64 Docker images). These are observed database semantics, not results from the work application's source code or production database.

| Question | Observation |
| --- | --- |
| Can a time-partitioned parent keep `PRIMARY KEY(id)`? | Rejected, SQLSTATE `0A000`. |
| Can a child reference only `id` on the composite-key parent? | Rejected, SQLSTATE `42830`. |
| Does an identity column work on a partitioned parent? | Yes on both tested versions when inserting through the parent. Ordinary explicit IDs were rejected with `428C9`. Direct-to-leaf behavior differs; see the version matrix. |
| Can the queue preserve one-to-one semantics? | Yes per composite parent identity, using `UNIQUE(event_id,parent_scheduled_at)`. |
| Can scheduling change independently? | Yes. Updating the queue's own time left it in the partition selected by its copied parent time. |
| Does the composite key preserve bare-ID uniqueness? | No. An explicit identity override produced two rows with the same ID and different timestamps. |
| Can a referenced parent partition detach? | Ordinary detach was rejected with `23503`. Concurrent detach is not covered. |
| Does DROP retire children? | Restrictive DROP was rejected with `2BP01`. DROP CASCADE removed constraints and left child rows; ordinary DELETE cascaded to children. |
| Does a rename switch existing FKs to the new table? | No. The FK remained attached to the old table object after rename. |
| Is retaining the old table sufficient rollback? | No. The old copy lacked a write made after cutover. |

The copy-keepers fixture retained a related set across parent, queue, event, and soft-history tables under a full writer freeze. It proves that this small construction works; it does not measure a tolerable freeze duration or solve online synchronization. The local CSV check compared all values and duplicate multiplicities after restoration.

## Decisions not made

Neither full partitioning nor a payload/identity split is selected for production. The required retention clock, allowable over-retention, global identity contract, history policy, permitted write freeze, and external writers remain inputs to that decision. A payload split still requires moving existing data and measuring the full cascade/delete workload.

## Aurora proof, October 8, 2026

Aurora PostgreSQL 17.9 with `aws_s3` 2.0 and `pg_partman` 5.2.4 passed all nine cloud checks. Two exports contained 100 rows each and totaled 5,297,452 bytes. Re-imported parent and queue records matched their sources with full SQL multiset comparison. Deliberate same-count content corruption was detected. A failed export left the source intact. After verification, child-first retirement removed the expired partitions and preserved the live pair; an independent request after commit confirmed that state. S3 object metadata confirmed SSE-KMS encryption.

Manual pg_partman maintenance created partitions without its background worker. No automatic retention or cron schedule was enabled. The full tiny run took 24.88 seconds including CLI/API overhead; this is not an export-throughput benchmark. Public-safe evidence is in `evidence/aurora17.json`; the [version matrix](version-matrix.md) separates cloud and local coverage.

This establishes export fidelity and retirement only for the modeled parent/queue fixture under exclusive locks. It does not establish compliance, production throughput, or application compatibility.
