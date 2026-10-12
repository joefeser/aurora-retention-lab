# Aurora PostgreSQL 16 → 17 upgrade timing

This extension measures a **quiescent in-place Aurora PostgreSQL upgrade**. It is not an RDS PostgreSQL instance upgrade, blue/green deployment, logical migration, or production downtime guarantee. It does not test partition DDL. Original documents and real data remain outside Git.

## Findings from the completed upgrade matrix

All **six authorized follow-up upgrades passed** from Aurora PostgreSQL 16.15 to 17.11, across measured databases of **0.422–6.685 GiB**. Fixed 4 ACU and `db.r6g.large` each completed 1x, 4x and 16x. Observed upgrade elapsed times ranged from **7 minutes 31 seconds to 9 minutes 2 seconds**. The original 0.5–2 ACU baseline failed and is preserved below.

| Team question | Evidence-backed answer |
| --- | --- |
| Is a ≤1 hour upgrade realistic at these dev/stage sizes? | Every successful managed-upgrade measurement was under 10 minutes for this quiescent synthetic fixture. This supports feasibility at the tested sizes/configurations, not a guaranteed production or end-to-end application maintenance window. Loading, application checks and post-upgrade statistics work are outside the upgrade timer. |
| Do we need more capacity? | Fixed 4 ACU was sufficient in all three tests after the 0.5–2 ACU attempt failed during managed source-server startup. This does not establish the minimum required capacity or prove a live resize/downscale procedure. |
| Does instance size materially change the duration? | These single observations do not demonstrate a consistent material advantage for either tested capacity model. Serverless elapsed times were non-monotonic across data sizes; repeat runs would be needed to estimate distributions or attribute differences to capacity. |
| Is the upgrade uninterrupted? | No: every case recorded failed Data API SQL probes. Successful cases had sampled interruption upper envelopes of about 7–8 minutes. Direct TCP/application downtime was not measured. |
| Did the graph survive? | All five row counts and per-table total/heap/index byte measurements matched before and after every successful upgrade. Database-wide size changed slightly. This is not payload checksum or application-behavior validation. |

The matrix completes the requested bounded version-upgrade experiment. It does not add partition operations, production traffic, extensions, failover testing or production-specific parameter groups. Use the detailed receipts and limitations below when planning a real maintenance window.

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

The original runner correctly refused to count `available` on 16.15 as success, but continued polling. After inspecting the explicit AWS failure event, the operator interrupted polling and added the diagnostic and post-rollback checks to the receipt. Its `KeyboardInterrupt` records that intervention; `AWSUpgradeFailure` records the service failure. A subsequent runner change detects current-attempt failure events on rollback and stops earlier. The original execution commit and hashes remain in the receipt. Its publication stage was subsequently reconciled to `failed_rollback` using that preserved failure event and verified source-version recovery; original error stages remain unchanged. Raw service logs and identifiers remain private.

The old engine became available after rollback, so an `available` status alone would have produced a misleading success. This run also disproves an uninterrupted-online interpretation for this attempt; it cannot establish successful-upgrade outage duration.

Cleanup completed on October 12: the stack reached `DELETE_COMPLETE`; independent reads confirmed the cluster, writer and managed secret absent, and zero cluster snapshots. Raw logs and the ownership inventory are retained only locally. AWS billing can lag resource deletion; the $6 run allowance is not a measured invoice.

## Authorized capacity follow-up

**6 of 6 follow-up upgrades passed; 6 have verified stack/snapshot cleanup in their published receipts.** The original 0.5–2 ACU failure remains a separate observation. All follow-up cases use fresh Aurora 16.15 clusters and target 17.11.

| Configuration | Tier | Before GiB | After GiB | Load seconds | Upgrade seconds | API gap upper seconds | Outcome |
| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| Fixed 4 ACU | 1x | 0.422 | 0.422 | 62.37 | 481.63 | 480.32 | Passed |
| Fixed 4 ACU | 4x | 1.668 | 1.668 | 226.01 | 541.99 | 480.33 | Passed |
| Fixed 4 ACU | 16x | 6.685 | 6.685 | 887.66 | 451.41 | 420.21 | Passed |
| db.r6g.large | 1x | 0.422 | 0.422 | 79.08 | 451.49 | 450.14 | Passed |
| db.r6g.large | 4x | 1.668 | 1.668 | 298.08 | 482.14 | 480.62 | Passed |
| db.r6g.large | 16x | 6.685 | 6.685 | 1,168.63 | 481.71 | 480.29 | Passed |

Values are rounded here; [the receipts](../evidence/upgrade-timing.json) contain exact byte counts, UTC timestamps, all probes and per-table measurements. Upgrade completion requires both target-version control-plane availability and SQL; SQL can recover before AWS finishes reporting the upgrade as complete. The approximately 30-second polling cadence limits timing precision.

Every passing case preserved all five row counts. The per-table total, heap and index byte measurements also matched before and after for the completed cases; `pg_database_size()` changed slightly. This is not a payload-byte checksum comparison.

The timings are individual observations, not a latency distribution or a causal instance-size estimate. The non-monotonic serverless results show why these data should not be extrapolated as a size-to-duration formula. The original low-capacity 4x and 16x cases remain intentionally unmeasured after the baseline failure; they are not part of the authorized six-case follow-up.

After reviewing the failed baseline, the owner authorized a higher-capacity test and completion of the process. The follow-up configuration `serverless_4acu` holds both minimum and maximum at 4 ACU; it is distinct from the original `serverless_v2` 0.5–2 ACU case. Fresh clusters remain required per case. The authorized follow-up covers 1x, 4x and 16x on fixed 4 ACU and on the provisioned class, subject to the shared budget. The failed receipt remains published.

[AWS capacity guidance](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/aurora-serverless-v2.setting-capacity.html) recommends raising the minimum and/or maximum for memory pressure and notes that Aurora manages `shared_buffers` dynamically. Holding 4 ACU avoids a low idle capacity; it does not guarantee that the underlying upgrade failure is fixed.

New attempts reserve $5 each, while the original attempt retains its $6 reservation: the prior $11.60 allowance plus $6 plus six new $5 reservations totals $47.60. At the verified $0.12/ACU-hour rate, fixed 4 ACU costs $0.48/hour. Approximately 6.25 hours of bounded phase waits would allow $3 compute, leaving $2 per run for other usage; this is planning, not an enforced cap. Review usage and stop before exceeding the overall $50 constraint.

## Final resource verification

At **2026-10-12T22:16:17.979308+00:00**, independent AWS reads verified all seven attempt inventories: every owned stack was `DELETE_COMPLETE`, every recorded cluster and writer was absent, every managed secret was absent, and each cluster had zero remaining snapshots. This includes the failed baseline and all six successful follow-up cases. The sequential controller exited successfully. No known resources from these seven attempts remain; billing settlement can lag deletion.

## Experimental design

Use a fresh PostgreSQL 16 cluster for every size/configuration. The original retention lab, including its VPC and subnet group, was verified deleted on October 9. `infra/lab.json` therefore adds an opt-in `LabMode=upgrade` with `UpgradeCluster`/`UpgradeWriter` and recreates the same isolated network layout. It does not recreate the old PostgreSQL 17 cluster or its S3 archive resources. Each run has a distinct stack and private ownership inventory under `.lab/upgrade-timing/`; `.lab/aws.json` remains the original lab inventory.

| Tier | Parents / queues | Push rows | History rows | Templates |
| --- | ---: | ---: | ---: | ---: |
| 1x | 32,768 each | 98,304 | 8,192 | 1 |
| 4x | 131,072 each | 393,216 | 32,768 | 1 |
| 16x | 524,288 each | 1,572,864 | 131,072 | 1 |

The existing `benchmarks/workload.py` **typical** profile supplies the five relations, payloads, indexes, keys and FKs. Child projections are unchanged and their source rows are bounded into non-overlapping chunks. No rows are retired. No schema migration, partition creation or ALTER TABLE occurs in the upgrade window.

The prior ~425 MiB figure and its multiples are sizing expectations, not acceptance targets. We do not pad values, disable compression, or manufacture uncompressible data to hit them. `pg_database_size()` and each table's `pg_total_relation_size()`, heap size and index size are recorded before/after. Total relation size naturally includes TOAST; it must not be described as uncompressed logical HTML size. Compression settings are recorded, and comparisons use measured sizes rather than inferred multiples. Database size includes catalogs and other database overhead.

Configurations are the original Serverless v2 case (`db.serverless`, 0.5–2 ACU), the authorized fixed-4-ACU follow-up (`db.serverless`, minimum and maximum both 4), and provisioned `db.r6g.large`, each with one private writer and no reader. Capacity is configured when each fresh cluster is created. They are different capacity models, not equal hardware. One run per case is an observation, not a latency distribution or proof of a causal instance-size effect.

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
# AWS CLI installed and authenticated; benchmark venv with benchmarks/requirements.txt.
.lab/perf-venv/bin/python scripts/run-upgrade-timing.py run \
  --tier 1x --config serverless_4acu --source 16.15 --target 17.11 \
  --prior-spend-usd 11.60
```

The `run` action uses POSIX file locking and requires macOS, Linux or WSL; publication and cleanup do not import the locking module. It checks that the AWS CLI is installed before provisioning. For a fresh checkout, create a Python virtual environment and install `benchmarks/requirements.txt`, then supply `--expected-account YOUR_INTENDED_12_DIGIT_ACCOUNT_ID`. The runner checks the authenticated STS account against that explicit value before provisioning. Without this option, an existing private `.lab/aws.json` inventory is required. Account values and credentials belong outside Git. Use `--config serverless_4acu` for the authorized fixed-4 follow-up. A process lock serializes launch, reservation and execution; an interrupted process releases the lock but its retained inventory still requires inspection/cleanup.

Replace tier/config only after reviewing the prior run and remaining budget. The prior-spend figure is a conservative October 12 readback of Ohio RDS/S3/Secrets Manager/KMS/CloudWatch charges since October 8, including unrelated account activity; it is not an exact lab invoice. It must be refreshed for later reuse. Current regional compute rates read from AWS Pricing were $0.12/ACU-hour ($0.24/hour at the original 2-ACU maximum; $0.48/hour at fixed 4 ACU) and $0.26/hour for standard Aurora `db.r6g.large`; storage, I/O, snapshots, secret/API usage and tax are additional.

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

Pending or unknown outcomes and failures before any upgrade request cannot be published as measured upgrade failures. Failed publication requires a reconciled terminal failure or rollback stage; a timeout or transport exception alone is insufficient. Unsupported receipt schemas and malformed diagnostic/probe envelopes are rejected before replacing existing evidence. The public file lists cases not measured; absence is not a successful result. Each receipt records its execution commit and source hashes because subsequent documentation/publication changes may be newer than the actual benchmark code.

### Review the upgrade evidence without AWS

Start with the six-case table above, then inspect `evidence/upgrade-timing.json`. For each passing case, check both SQL and control-plane versions, the five before/after row counts, measured bytes, upgrade timestamps and probe sequence. The original failed attempt must remain a failure; the two larger low-capacity cases are explicitly unmeasured.

Each `git_head` identifies execution code, not the later publication commit. With the repository history available, verify each recorded `source_hashes` entry against the SHA-256 of `git show <git_head>:<path>`. The source files are the runner, workload generator and infrastructure template. A later reporting fix does not mean an earlier cloud run used that fix. These checks establish internal consistency and code provenance; they do not independently authenticate historical AWS execution or reproduce its timing.
