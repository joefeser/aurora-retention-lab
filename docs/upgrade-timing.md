# Aurora PostgreSQL 16 → 17 upgrade timing

This extension measures a **quiescent in-place Aurora PostgreSQL upgrade**. It is not an RDS PostgreSQL instance upgrade, blue/green deployment, logical migration, or production downtime guarantee. It does not test partition DDL. Original documents and real data remain outside Git.

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

Replace tier/config only after reviewing the prior run and remaining budget. The prior-spend figure is a conservative October 12 readback of Ohio RDS/S3/Secrets Manager/KMS/CloudWatch charges since October 8, including unrelated account activity; it is not an exact lab invoice. It must be refreshed for later reuse. Current regional compute rates read from AWS Pricing were $0.12/ACU-hour (at most $0.24/hour here) and $0.26/hour for standard Aurora `db.r6g.large`; storage, I/O, snapshots, secret/API usage and tax are additional.

The harness reserves $6 per attempted case against the total $50 budget and never refunds that reservation automatically based on delayed billing. This is an operational planning allowance, **not an AWS-enforced spending cap**. It rejects another case while any earlier owned run remains retained. Creation, loading, upgrade and cleanup have bounded waits. Review actual usage/cost between cases and stop the matrix if the allowance becomes insufficient.

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
