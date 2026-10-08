# PostgreSQL capability matrix

Version 17 was initially selected from claims in the supplied research, not from a verified production engine inventory. The work database's exact version remains unconfirmed. User authorization permits testing Aurora 16 as well if an AWS-specific comparison requires it.

Local results below were executed on PostgreSQL **16.15** and **17.11**, not inferred from version labels. Both passed all 19 fixture assertions. Exact image IDs, engine strings, and SQLSTATEs are recorded in `evidence/local.json`.

| Capability / claim | Local 16.15 | Local 17.11 | Implication |
| --- | --- | --- | --- |
| Native `ALTER TABLE ... SPLIT PARTITION` shown in screenshots | Syntax error `42601` | Syntax error `42601` | Upgrading to 17 does not make that migration command available. |
| Time-partitioned `PRIMARY KEY(id)` | Rejected | Rejected | Version 17 does not remove the partition-key requirement. |
| ID-only FK to composite-key parent | Rejected | Rejected | Key redesign remains necessary. |
| `GENERATED ALWAYS AS IDENTITY` on parent, insert through parent | Works; explicit ID rejected | Works; explicit ID rejected | Claim that parent identity requires 17 is incorrect. |
| Omitted ID on direct insert into leaf | NOT NULL violation `23502` | ID generated | 17 improves identity inheritance on leaves. |
| Explicit ID on direct insert into leaf | Accepted | Rejected `428C9` | Parent ALWAYS enforcement does not protect direct leaf writes in tested 16. |
| One queue per composite parent | Enforced | Enforced | `UNIQUE(event_id,parent_scheduled_at)` works when partitioned by that timestamp. |
| Mutable queue scheduling with immutable parent partition key | No partition move | No partition move | Scheduling and retention identity can be separated. |
| Global bare-ID uniqueness from composite PK | Not enforced | Not enforced | Shared sequence is not an ID uniqueness constraint. |
| Ordinary detach with outstanding child rows | Rejected `23503` | Rejected `23503` | Retire dependencies explicitly. |
| Restrictive DROP with dependent FKs | Rejected `2BP01` | Rejected `2BP01` | DDL does not act as row-level cascade. |
| DROP CASCADE | Removes dependent constraints, leaves rows | Same | Unsafe retirement shortcut. |
| Existing FKs automatically follow table-name swap | No | No | Rebuild/redirect relationships deliberately. |

## AWS evidence boundary

Aurora PostgreSQL 17.9 passed the cloud fixture with `aws_s3` 2.0 and `pg_partman` 5.2.4. Aurora 16 has not been provisioned. Evidence: `evidence/aurora17.json`. Local checks do not establish Aurora extension support or behavior.

| Cloud capability | Aurora 16 | Aurora 17.9 |
| --- | --- | --- |
| S3 export and re-import, exact content comparison | Not run | Passed for 100 parent and 100 queue rows |
| Destination-denied export preserves source contents | Not run | Passed against the actual source partition |
| Detect same-count corrupted restore | Not run | Passed |
| Child-first retirement preserves live pair | Not run | Passed; fresh post-commit request confirmed old partitions absent |
| pg_partman without background worker | Not run | Manual maintenance passed, extension 5.2.4 |
| S3 export encryption | Not run | SSE-KMS verified on both objects |

Concurrent detach, EF mappings, RDS Proxy, production throughput, and direct-leaf identity behavior on Aurora are not covered by the AWS fixture. Scheduled maintenance was not configured; the proof calls maintenance manually with automatic retention disabled.

Phase 2's [cooperative migration rehearsal](phase-2.md) also passed seven assertions on both local 16.15 and 17.11. The 128 MiB and 1 GiB S3 baselines ran only on Aurora 17.9; there is no Aurora 16 performance comparison.

Phase 3's [archive-verified retirement comparison](phase-3.md) passed two trials each of indexed cascade deletion and coordinated partition retirement on Aurora 17.9. Aurora 16 was not run; these small timings do not establish a version or production performance advantage.

Phase 4's [live archival fixture](phase-4.md) passed twice on Aurora 17.9: live inserts and reads progressed under expired-leaf SHARE locks, mutation probes were blocked, and acknowledged relationships survived retirement. Aurora 16 and general live UPDATE/DELETE behavior remain untested.

## References

- [PostgreSQL 16 CREATE TABLE](https://www.postgresql.org/docs/16/sql-createtable.html)
- [PostgreSQL 17 identity columns](https://www.postgresql.org/docs/17/ddl-identity-columns.html)
- [PostgreSQL 17 ALTER TABLE syntax](https://www.postgresql.org/docs/17/sql-altertable.html)
- [PostgreSQL 17 partitioning constraints](https://www.postgresql.org/docs/17/ddl-partitioning.html)
