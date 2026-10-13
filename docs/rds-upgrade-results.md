# Standard RDS PostgreSQL 16 → 17 timing results

2/4 planned synthetic cases completed and cleaned. Unmeasured: large on db.r7g.xlarge, medium on db.r7g.large. These are standard RDS (`Engine=postgres`) measurements; the separate Aurora receipts are not used in this report.

## Measurements

One trial per case, PostgreSQL **16.15 → 17.11**, Ohio (`us-east-2`), Single-AZ, encrypted **400 GiB gp3**, observed **12,000 IOPS / 500 MiB/s**. The instance classes use Graviton3. Times are minutes:seconds, rounded for readability. Allocated storage and measured database bytes are different quantities.

| Fixture | Class / RAM / vCPU | Database GiB | Load | Request → ready | Largest SQL interruption upper bound | Total incl. setup/teardown |
|---|---|---:|---:|---:|---:|---:|
| small | db.r7g.xlarge / 32 / 4 | 0.411 | 0:18 | 6:32 | 3:03 | 18:01 |
| medium | db.r7g.xlarge / 32 / 4 | 6.455 | 4:06 | 7:53 | 2:33 | 28:16 |

Request-to-readiness ends only after AWS reports the target version available and a fresh PostgreSQL connection has returned the target version. SQL interruption is a last-success to first-success observation envelope, not an exact outage duration. The full receipts retain every episode; the table shows the largest bound if there are multiple episodes. No failed sample would not prove zero downtime between probes.

[Full sanitized receipts](../evidence/upgrade-timing-rds.json) contain all probes, AWS status samples, five-table byte measurements, constraints, sampled content hashes, configuration and code hashes. [CSV](report/rds-upgrade-timing.csv) retains unrounded values. [Service-log observations](../evidence/rds-upgrade-log-observations.json) retain safe phase labels and raw-capture hashes; raw logs remain private.

## What the experiment tests

The small, medium and large fixtures retain the same five-table schema, indexes, constraints and data-generation rules while increasing parent counts. This tests size sensitivity for a fixed object shape. It does not vary catalog complexity, database count, extensions, partition count, replication or concurrent application activity. The 16 GiB comparison changes both RAM and vCPU count; it cannot isolate a RAM-only effect.

The RDS service logs captured for the initial cases report `Linking user relation files` and `Upgrade Complete`. This supports AWS's documented use of `pg_upgrade --link`, which avoids a full data-file copy during that step. It does not mean snapshots, preparation, restart, managed recovery or subsequent statistics work take no time. [AWS object-count guidance](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/PostgreSQL.HighObjectCount.html).

A proportional stored-bytes model is therefore not a justified way to predict production downtime. These measurements can challenge that model for this fixture, but one trial per case cannot establish a reliable slope, variance or confidence interval. A rehearsal of the actual environment is still needed for an operational maintenance window.

## Data checks and cleanup

Every published case preserved expected relation counts, first/last-row content hashes, constraints and the target SQL version. Recorded heap, index and total bytes were unchanged in **2/2** cases. These are counts and sampled content checks, not a full content checksum or application compatibility test.

| Fixture / RAM | Parents | Queue | Push children | History | Templates |
|---|---:|---:|---:|---:|---:|
| small / 32 GiB | 32,768 | 32,768 | 98,304 | 8,192 | 1 |
| medium / 32 GiB | 524,288 | 524,288 | 1,572,864 | 131,072 | 1 |

The harness verified each published stack deleted, database absent, snapshots absent and managed secret absent before admitting another case. Independent retained-backup verification is separate from an AWS deletion request. Resource absence is not a confirmation that delayed billing reports have settled.

## Cost and reproduction

Using the dated [AWS pricing receipt](../evidence/rds-instance-pricing.json) and the full recorded lifecycle duration gives an approximate compute-plus-allocated-storage estimate of **$0.42** for the published cases. This is an elapsed-time estimate, not an invoice, and excludes snapshot storage, public IPv4, Secrets Manager, transfer and taxes. The batch's admission reservations total at most $30 and are not an AWS-enforced spending cap.

Follow the [run guide](rds-upgrade-timing.md) to reproduce the selected synthetic cases. PostgreSQL 16.8 was reported deprecated and rejected for the selected new-instance provisioning combination in our dated Ohio API check; these tests use 16.15 and do not measure a 16.8 upgrade. The actual deprecation date is unknown. [Version/orderability receipt](../evidence/rds-version-orderability.json).

Regenerate this report offline with `python3 scripts/report-rds-upgrade-timing.py`. The script reads public receipts and pricing only and makes no AWS calls. Execution commits are preserved separately from report-generation code:

- small / db.r7g.xlarge: `fc5b81c3789fd878c9fef98b07015a6726870891`
- medium / db.r7g.xlarge: `22efd30c1f726c6580e78dab4bcc15ead80dc4ab`

The fixture, infrastructure and probe logic are fixed across these cases; intervening harness changes tightened version/export guards and retained budget reservations. Service-log collection was made repeatable after the initial two manual API captures. Consult each receipt's source hashes rather than assuming the current checkout is the execution version.

## What remains internal

An actual-data snapshot rehearsal and a cleanup-versus-no-cleanup comparison belong in the approved internal environment. This synthetic study does not establish that deleting a large payload table will halve downtime. Cross-region active-active behavior, application recovery, custom parameters, logical replication slots and extension upgrades were not exercised. Keep private identifiers, aggregate counts and real data outside this public repository.
