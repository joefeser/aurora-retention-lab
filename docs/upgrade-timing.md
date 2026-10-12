# Aurora PostgreSQL 16 → 17 upgrade timing

This extension measures a **quiescent in-place Aurora PostgreSQL upgrade**. It is not an RDS PostgreSQL instance upgrade, blue/green deployment, logical migration, or production downtime guarantee. It does not test partition DDL. Original documents and real data remain outside Git.

## Original October 12 attempt: failed low-capacity baseline

The original Config A / 1x attempt failed inside AWS-managed `pg_upgrade` and returned to 16.15. We stopped that matrix and preserved the failure in the [published receipts](../evidence/upgrade-timing.json). The owner then authorized the higher-capacity follow-up below. This original attempt supplies no successful-upgrade timing.

| Measurement | Observed result |
| --- | --- |
| Requested version change | Aurora 16.15 → 17.11 |
| Instance configuration | Serverless v2, 0.5–2 ACU |
| Parents / queues | 32,768 each, before and after rollback |
| Database size | 453,487,191 bytes (432.48 MiB), unchanged after rollback |
| Five-table total relation size | 445,308,928 bytes (424.68 MiB) |
| Load elapsed | 78.24 seconds |
| Upgrade request start | 2026-10-12 18:15:14.160 UTC |
| AWS upgrade failure event | 2026-10-12 18:20:21.707 UTC |
| First sampled SQL recovery on **16.15** | ~361.54 seconds after request |
| Failed Data API probes | 11 |
| Sampled API interruption upper envelope | 360.23 seconds; not direct application downtime |
| Successful upgrade elapsed | **Not available: upgrade failed** |

The CLI accepted the request (exit 0, empty stderr). AWS events then reported successful online prechecks, taking the cluster offline, creating a pre-upgrade snapshot, cloning the volume, upgrading the writer, and failure. The managed upgrade server log contains this diagnostic, preserved verbatim apart from its timestamp/prefix:

```text
FATAL:  shared memory segment sizes are configured too large; total requested: 6330675200 (buffer cache: 40960, main segment: 5761819280, 65536 buffers), system total memory: 5158047744.
HINT: try reducing shared memory parameters, such as shared buffers.
```

This establishes a source-server startup failure during `pg_upgrade`; it does not establish whether the root cause is Aurora configuration, capacity handling, or an engine defect. No custom parameter group was supplied. Increasing capacity or changing parameters would be a new experiment, not a correction to this failed receipt. All five post-rollback relation counts matched and physical database size was unchanged. Counts do not verify every payload byte.

The original runner correctly refused to count `available` on 16.15 as success, but continued polling. After inspecting the explicit AWS failure event, the operator interrupted polling and added the diagnostic and post-rollback checks to the receipt. Its `KeyboardInterrupt` records that intervention; `AWSUpgradeFailure` records the service failure. A subsequent runner change detects current-attempt failure events on rollback and stops earlier. The original execution commit and hashes remain in the receipt. Raw service logs and identifiers remain private.

The old engine became available after rollback, so an `available` status alone would have produced a misleading success. This run also disproves an uninterrupted-online interpretation for this attempt; it cannot establish successful-upgrade outage duration.

Cleanup completed on October 12: the stack reached `DELETE_COMPLETE`; independent reads confirmed the cluster, writer and managed secret absent, and zero cluster snapshots. Raw logs and the ownership inventory are retained only locally. AWS billing can lag resource deletion; the $6 run allowance is not a measured invoice.

## Authorized capacity follow-up

The first fixed-4-ACU baseline succeeded: **481.63 seconds (8 minutes 2 seconds)** from request to verified target-engine/SQL availability. All five counts matched. Database size was 453,478,999 bytes before and 453,412,083 after. The Data API interruption upper envelope was 480.32 seconds. This is one successful 1x observation, not a larger-tier or production-window guarantee. Its stack and upgrade snapshots were then verified deleted. The 4x case also succeeded at **541.99 seconds (9 minutes 2 seconds)** with 1,791,257,175 bytes before upgrade and all five counts preserved. Its API interruption upper envelope was 480.33 seconds; SQL recovered before AWS finished reporting the upgrade as complete. The remaining cases are still running.


After reviewing the failed baseline, the owner authorized a higher-capacity test and completion of the process. The follow-up configuration `serverless_4acu` holds both minimum and maximum at 4 ACU; it is distinct from the original `serverless_v2` 0.5–2 ACU case. Fresh clusters remain required per case. A passing baseline will be followed by the 4x and 16x tiers and the provisioned comparison while budget permits. The failed receipt remains published.

[AWS capacity guidance](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/aurora-serverless-v2.setting-capacity.html) recommends raising the minimum and/or maximum for memory pressure and notes that Aurora manages `shared_buffers` dynamically. Holding 4 ACU avoids a low idle capacity; it does not guarantee that the underlying upgrade failure is fixed.

New attempts reserve $5 each, while the original attempt retains its $6 reservation: the prior $11.60 allowance plus $6 plus six new $5 reservations totals $47.60. At the verified $0.12/ACU-hour rate, fixed 4 ACU costs $0.48/hour. Approximately 6.25 hours of bounded phase waits would allow $3 compute, leaving $2 per run for other usage; this is planning, not an enforced cap. Review usage and stop before exceeding the overall $50 constraint.

## Experimental design

Use a fresh PostgreSQL 16 cluster for every size/configuration. The original retention lab, including its VPC and subnet group, was verified deleted on October 9. `infra/lab.json` therefore adds an opt-in `LabMode=upgrade` with `UpgradeCluster`/`UpgradeWriter` and recreates the same isolated network layout. It does not recreate the old PostgreSQL 17 cluster or its S3 archive resources. Each run has a distinct stack and private ownership inventory under `.lab/upgrade-timing/`; `.lab/aws.json` remains the original lab inventory.

| Tier | Parents / queues | Push rows | History rows | Templates |
| --- | ---: | ---: | ---: | ---: |
| 1x | 32,768 each | 98,304 | 8,192 | 1 |
| 4x | 131,072 each | 393,216 | 32,768 | 1 |
| 16x | 524,288 each | 1,572,864 | 131,072 | 1 |

The existing `benchmarks/workload.py` **typical** profile supplies the five relations, payloads, indexes, keys and FKs. Child projections are unchanged and their source rows are bounded into non-overlapping chunks. No rows are retired. No schema migration, partition creation or ALTER TABLE occurs in the upgrade window.

The prior ~425 MiB figure and its multiples are sizing expectations, not acceptance targets. We do not pad values, disable compression, or manufacture uncompressible data to hit them. `pg_database_size()` and each table's `pg_total_relation_size()`, heap size and index size are recorded before/after. Total relation size naturally includes TOAST; it must not be described as uncompressed logical HTML size. Compression settings are recorded, and comparisons use measured sizes rather than inferred multiples. Database size includes catalogs and other database overhead.

Configurations are Serverless v2 (`db.serverless`, 0.5–2 ACU) and provisioned `db.r6g.large`, each with one private writer and no reader. They are different capacity models, not equal hardware. One run per case is an observation, not a latency distribution or proof of a causal instance-size effect.

## Version selection and measurement

The runner queries regional `ValidUpgradeTarget` and orderable instance options immediately before provisioning. It requires a direct upgrade from the selected ordinary PostgreSQL 16 release to the latest ordinary PostgreSQL 17 release available in Ohio. Preview/Limitless version labels are excluded. The October 12 discovery selected **16.15 → 17.11** for both configurations.

Use `SELECT version()` and `current_setting('server_version')`; PostgreSQL does not supply the requested `pg_version()` function as a standard SQL function.

The timing starts immediately before the CLI upgrade request. UTC timestamps accompany monotonic elapsed time. Describe and Data API SQL probes run approximately every 30 seconds, with request latency recorded. Success requires all of:

- Cluster status `available` **and the exact target engine version**.
- Writer status `available` and the exact target engine version.
- A successful SQL response from PostgreSQL 17.
- Post-upgrade counts matching all five pre-upgrade relations.

An initial `available` response from PostgreSQL 16 is not upgrade completion. Request acceptance, an unavailable SQL endpoint, target-engine availability, and smoke-check success are distinct evidence. Data API request failures are reported as a sampled API/SQL availability envelope. They do not independently measure direct TCP/Npgsql downtime, and no failed samples does not establish zero downtime. Polling/API latency limits timing precision. Counts and size queries after the first successful target-version probe are outside the upgrade elapsed measurement.

[AWS's major-upgrade guide](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/USER_UpgradeDBInstance.PostgreSQL.MajorVersion.html) describes valid target discovery, prechecks, the upgrade snapshot, and post-upgrade ANALYZE. The timing includes Aurora's upgrade machinery, including its automatic snapshot, but excludes loading and subsequent cleanup. This fixture uses no application extensions or production-specific parameter changes. Production application validation and post-upgrade statistics refresh require their own operating window.

## Run and cleanup

```sh
# Existing benchmark venv with benchmarks/requirements.txt; authenticated AWS default profile.
.lab/perf-venv/bin/python scripts/run-upgrade-timing.py run \
  --tier 1x --config serverless_v2 --source 16.15 --target 17.11 \
  --prior-spend-usd 11.60
```

For a fresh checkout, create a Python virtual environment and install `benchmarks/requirements.txt`, then supply `--expected-account YOUR_INTENDED_12_DIGIT_ACCOUNT_ID`. The runner checks the authenticated STS account against that explicit value before provisioning. Without this option, an existing private `.lab/aws.json` inventory is required. Account values and credentials belong outside Git. Use `--config serverless_4acu` for the authorized fixed-4 follow-up. A process lock serializes launch, reservation and execution; an interrupted process releases the lock but its retained inventory still requires inspection/cleanup.

Replace tier/config only after reviewing the prior run and remaining budget. The prior-spend figure is a conservative October 12 readback of Ohio RDS/S3/Secrets Manager/KMS/CloudWatch charges since October 8, including unrelated account activity; it is not an exact lab invoice. It must be refreshed for later reuse. Current regional compute rates read from AWS Pricing were $0.12/ACU-hour (at most $0.24/hour here) and $0.26/hour for standard Aurora `db.r6g.large`; storage, I/O, snapshots, secret/API usage and tax are additional.

The initial harness reserved $6 per attempted case; new follow-up attempts reserve $5 each against the total $50 budget and never refunds that reservation automatically based on delayed billing. This is an operational planning allowance, **not an AWS-enforced spending cap**. It rejects another case while any earlier owned run remains retained. Creation, loading, upgrade and cleanup have bounded waits. Review actual usage/cost between cases and stop the matrix if the allowance becomes insufficient.

Failed upgrades and unknown CLI outcomes are recorded without resubmitting the upgrade. Raw CLI stdout/stderr and failure details stay in the private run directory; public receipts contain typed failure metadata, not account IDs, ARNs or credentials. Failure stops that run and leaves it for inspection. There is no automatic retry of a failed case.

Successful runs are cleaned up after their receipts are saved. `--keep` retains a successful run explicitly and blocks the next case. A stopped or failed run can be removed with:

```sh
.lab/perf-venv/bin/python scripts/run-upgrade-timing.py cleanup RUN_ID
```

Cleanup verifies the exact stack ID and run tag, deletes that stack, and deletes only manual upgrade snapshots belonging to that run's unique cluster. It waits for stack deletion and absence of cluster snapshots. An interrupted cleanup retains the private inventory for a later explicit cleanup call. Never use this command on unrelated resources.

## Validation and reading results

```sh
python3 -m unittest discover -s tests
```

The loader was also exercised against a disposable local PostgreSQL 16 database with 128 parents: all five expected relation counts matched, physical-size queries succeeded, and its container was removed. This tests the fixture, not a managed Aurora major upgrade. Actual cloud outcomes belong in `evidence/upgrade-timing.json`; no local result substitutes for a timed cloud receipt.

Publish only explicitly selected finished receipts:

```sh
.lab/perf-venv/bin/python scripts/run-upgrade-timing.py publish RUN_ID [RUN_ID ...]
```

Pending runs cannot be published as failures or successes. The public file lists cases not measured; absence is not a successful result. Each receipt records its execution commit and source hashes because subsequent documentation/publication changes may be newer than the actual benchmark code.
