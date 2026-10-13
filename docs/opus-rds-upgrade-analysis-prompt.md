# Opus analysis prompt: standard RDS PostgreSQL 16 → 17 timing

Please assess whether our upgrade-window assumptions are supported by the new standard-RDS evidence. Use this public synthetic study alongside the private environment facts already available in this work workspace. Keep private data in the work environment. This request authorizes analysis and a proposed rehearsal plan, not AWS execution or production changes.

Repository: https://github.com/joefeser/aurora-retention-lab
Study PR: https://github.com/joefeser/aurora-retention-lab/pull/12

Start with `docs/rds-upgrade-results.md`, then `docs/rds-upgrade-timing.md`, `evidence/upgrade-timing-rds.json`, `evidence/rds-upgrade-log-observations.json`, and `scripts/run-rds-upgrade-timing.py`. These are **standard RDS**, not the earlier Aurora study. Check each execution commit and its source hashes; do not substitute the current checkout or Aurora timings for the recorded RDS execution.

Public synthetic measurements: 4/4 planned synthetic cases completed and cleaned.

| Fixture | Class / RAM GiB / vCPU | Database GiB | Load | Request → ready | Largest SQL interruption upper bound | Total incl. setup/teardown |
|---|---|---:|---:|---:|---:|---:|
| small | db.r7g.xlarge / 32 / 4 | 0.411 | 0:18 | 6:32 | 3:03 | 18:01 |
| medium | db.r7g.xlarge / 32 / 4 | 6.455 | 4:06 | 7:53 | 2:33 | 28:16 |
| large | db.r7g.xlarge / 32 / 4 | 25.800 | 16:15 | 12:42 | 3:03 | 43:31 |
| medium | db.r7g.large / 16 / 2 | 6.455 | 4:35 | 8:56 | 3:04 | 28:33 |

Configuration: PostgreSQL 16.15 → 17.11, Single-AZ Ohio, fixed 400 GiB gp3, observed 12,000 IOPS / 500 MiB/s. The baseline class has 32 GiB and 4 vCPUs; the comparison class has 16 GiB and 2 vCPUs. Each combination has one trial. The schema and fixture rules stay fixed while row counts increase. The case receipts include counts, measured bytes, sampled first/last-row hashes, constraints and cleanup checks.

Our dated Ohio API check found 16.8 reported deprecated and rejected for the selected new-instance provisioning combination. The exact deprecation date and snapshot-restore behavior are unknown. The lab consequently measured 16.15, not 16.8. This is not evidence that every PostgreSQL 16 release is unavailable or that existing instances immediately stop working.

The captured service logs say `Linking user relation files`. AWS documents `pg_upgrade --link`; a claim that this step must copy every table/TOAST byte is therefore unsupported. Managed preparation, backups, restart, recovery and statistics work remain separate. Request-to-AWS-readiness is different from the sampled SQL interruption envelope, and the minimal read probe does not measure application write availability or recovery.

Please return:

1. A team-ready answer separating measured facts, inferences and untested assumptions. Does a multi-hour downtime estimate follow from stored bytes alone? Do not replace it with a minutes-level production guarantee from these small fixed-object fixtures.
2. A comparison denominator using the private environment: actual RDS class and storage settings, topology, all databases, catalog/index/partition counts, extensions in every database, custom parameters, prepared transactions and logical replication slots. Explain the cross-region active-active mechanism rather than assuming its behavior. Keep those private identifiers and aggregate counts out of public artifacts.
3. A proposed internal Test A: restore an approved real-data snapshot into an isolated matching RDS configuration, record the actual restored version, document any hydration/warmup, perform the authorized upgrade, and independently measure read/write connectivity, managed-operation time, logs/events, post-upgrade analysis, application recovery and rollback/restore readiness. Account for the source-minor substitution if exact 16.8 restoration is unavailable.
4. A proposed internal Test A2 from the same snapshot/configuration, with an approved payload-retention cleanup before the upgrade. Validate dependencies and retention requirements first. Do not assume cleanup is safe or that it halves downtime; distinguish upgrade, backup and post-upgrade maintenance benefits. TRUNCATE already reclaims its table storage; subsequent VACUUM is not required merely to reclaim that storage.
5. Remaining blockers, and the smallest additional evidence needed for an operational maintenance window. The class comparison changes CPU and RAM together, and one trial per case gives no variance estimate. Extension support in PostgreSQL 17 alone does not prove an environment has no upgrade blockers.

Use precise private facts for the internal team analysis. If returning a summary for the public repository, sanitize it: no company names, internal schema/table names, account IDs, endpoints, private aggregate counts, raw screenshots or real data. Public synthetic fixture values above are safe to reuse.
