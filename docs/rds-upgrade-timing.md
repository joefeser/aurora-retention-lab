# Standard RDS PostgreSQL upgrade timing: run guide

Status: all four planned synthetic cases are complete and cleaned, including the 16 GiB instance-class comparison. See the [results report](rds-upgrade-results.md) and [Opus handoff](opus-rds-upgrade-analysis-prompt.md). Production rehearsal remains untested. The existing Aurora receipts remain Aurora-only evidence.

## Selected version and instance

Use standard RDS for PostgreSQL (`Engine=postgres`) in `us-east-2`, starting at **16.15** and upgrading to **17.11**. The baseline instance is `db.r7g.xlarge` (32 GiB RAM). Read-only AWS API checks on **October 12, 2026** confirmed orderable options for this source version/class and the direct 17.11 upgrade target. Recheck immediately before provisioning.

## PostgreSQL 16.8 provisioning limitation

On October 12, 2026, the Ohio RDS API returned:

The sanitized [version/orderability receipt](../evidence/rds-version-orderability.json) records these API observations without account identifiers.

- `DescribeDBEngineVersions`, with `Engine=postgres`, `EngineVersion=16.8`, and `IncludeAll=true`: `Status=deprecated`.
- `DescribeOrderableDBInstanceOptions`, with `Engine=postgres`, `EngineVersion=16.8`, and `DBInstanceClass=db.r7g.xlarge`: `InvalidParameterCombination`, with the message `Cannot find version 16.8 for postgres`.
- The 16.8 engine metadata still listed upgrade targets including 17.11. Upgrade targets for an existing instance and availability for new provisioning are different facts.

**Planning implication:** we could not provision a fresh RDS PostgreSQL 16.8 baseline using the tested class in Ohio. Reproducing that exact minor version is therefore already constrained. The synthetic study will use 16.15 and must not be described as a measured 16.8 upgrade.

This is a dated, region-specific API observation. It does not establish availability in every region, behavior of every snapshot-restore path, an immediate shutdown of existing 16.8 instances, or a mandatory upgrade deadline. Any snapshot-based rehearsal must record the actual engine version after restoration.

Shareable wording:

> Our October 12 check found AWS reporting RDS PostgreSQL 16.8 as deprecated in Ohio and rejected our new-instance orderability check. We are using the available 16.15 release for the synthetic upgrade benchmark. This makes exact-version rehearsal harder and is a reason to prioritize upgrade planning; it is not evidence that existing instances will immediately stop working.

## Test boundaries

- Start with the small and medium synthetic fixtures on the same instance class and storage configuration. Review measured downtime, total runtime and remaining budget before launching larger cases.
- Use a $30 planning limit for the new synthetic batch, reserving room for cleanup and ancillary costs. This is not an AWS-enforced billing cap.
- Measure direct PostgreSQL connectivity/query interruption separately from request-to-target-version readiness, loading, validation and cleanup. AWS status transitions alone do not define SQL downtime.
- RDS uses `pg_upgrade --link` to avoid copying data files; do not assume downtime grows linearly with stored bytes. Object counts and configuration also matter. See [AWS's object-count guidance](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/PostgreSQL.HighObjectCount.html).
- Keep source and target versions, measured bytes, synthetic counts, instance/storage settings and execution provenance in each future receipt.
- Real-data snapshot rehearsals belong in the approved internal environment. No internal identifiers, row counts, source documents or real data belong in public artifacts.

## Running the synthetic cases

The separate harness uses standard RDS, not Aurora or the Data API. It creates a disposable Single-AZ `db.r7g.xlarge`, encrypted 400 GiB gp3 storage, and an RDS-managed master secret. It uses a public endpoint with PostgreSQL ingress restricted to the operator's current IPv4 `/32`, TLS `verify-full`, and the AWS CA bundle. It loads only this repository's synthetic fixture. There is no SSH, NAT gateway, or access from other client addresses. Changing networks during a run can cause probe failures unrelated to the engine upgrade.

The 400 GiB allocation holds the gp3 baseline at 12,000 IOPS / 500 MiB/s across fixture sizes; actual AWS settings are recorded. Allocated capacity is distinct from measured database bytes. The baseline is approximately $0.54 per database-hour at the checked Ohio compute/storage prices, excluding snapshots, transfer and taxes. A $30 planning budget is not an enforced AWS spending ceiling.

Prerequisites: AWS CLI authentication in the default profile, the existing private `.lab/aws.json` account inventory, and the Python dependencies in `benchmarks/requirements.txt`. Download the public AWS certificate bundle:

For a fresh clone, create the ignored `.lab/aws.json` with an `account` property containing the expected 12-digit **lab** account ID. The harness compares that expectation with STS before provisioning. Linux, macOS or WSL is required for its file lock. Prepare the Python environment with `python3 -m venv .lab/perf-venv` and `.lab/perf-venv/bin/pip install -r benchmarks/requirements.txt`.

```sh
mkdir -p .lab/rds-upgrade-timing
curl -fsSL https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem \
  -o .lab/rds-upgrade-timing/global-bundle.pem
```

Commit benchmark sources before execution; each private receipt records their hashes and Git commit. Run cases individually:

```sh
.lab/perf-venv/bin/python scripts/run-rds-upgrade-timing.py run --tier small
.lab/perf-venv/bin/python scripts/run-rds-upgrade-timing.py run --tier medium
```

Each case tears down its own resources in a `finally` block. A failed cleanup or an interrupted process leaves a durable private inventory and blocks the next run. The explicit recovery command is:

```sh
.lab/perf-venv/bin/python scripts/run-rds-upgrade-timing.py cleanup --run-id RUN_ID
```

Use the exact run ID from `.lab/rds-upgrade-timing/*/inventory.json`. Do not delete an inventory before reconciling it. Cleanup requires exact stack tags, database membership and authenticated account identity, then checks that the stack, database, snapshots and managed secret are gone. Unknown API outcomes require inspection; the harness never automatically retries the major upgrade.

The runner reserves $7 for each new attempted case, retains the original $8 reservations for the initial two cases, refuses overlapping runs and stops admission above the $30 batch planning budget. The four-case allocation is therefore $8 + $8 + $7 + $7 = $30, including allowance for cleanup and ancillary costs. This is a conservative reservation, not a cost meter. Fixture loading has a one-hour deadline checked between statements; upgrade observation defaults to one hour. These are observation limits, not a promise that AWS operations or deletion will finish within them. An attempted case counts against the reservation even if it fails.

Review the first two receipts before deciding on the 2,097,152-parent large case or an instance-class comparison. The fixtures use 32,768, 524,288 and 2,097,152 parents respectively. Five relation counts, sampled first/last-row content hashes and constraints are checked before and after. This is not a full database content checksum or application compatibility test.

Probes open a fresh TLS connection and execute `SELECT 1` plus a version query. They wait five seconds between completed attempts; connection and query time make the actual interval longer during failures. Each interruption has a separate last-success / first-success envelope, with unbounded edges labeled. Twenty-second control-plane polling separately bounds request-to-target-readiness. AWS events remain private, while their timestamps/categories can be published. Neither mechanism isolates the `pg_upgrade` phase. A minimal read probe does not measure write availability or application recovery.

While an owned instance still exists, capture its service logs from a separate terminal:

```sh
.lab/perf-venv/bin/python scripts/capture-rds-upgrade-logs.py --run-id RUN_ID
```

Run this after the target engine starts responding and before teardown. The collector verifies ownership, saves bounded raw captures privately, and exports only exact phase labels such as `Linking user relation files`. The initial two cases used the same APIs manually; later captures record the collector commit and source hash. Public observations are in `evidence/rds-upgrade-log-observations.json`. Captured phase labels do not define SQL downtime.

For independent cleanup verification, inspect retained automated backups using the original `DbiResourceId` or enumerate and match the recorded unique instance identifiers. `DescribeDBInstanceAutomatedBackups` with its `DBInstanceIdentifier` request parameter requires an existing instance, so a not-found response after deletion is insufficient evidence. See the [AWS API contract](https://docs.aws.amazon.com/AmazonRDS/latest/APIReference/API_DescribeDBInstanceAutomatedBackups.html).

After all selected cases have passed and cleanup is verified, publish the sanitized receipts:

```sh
.lab/perf-venv/bin/python scripts/run-rds-upgrade-timing.py publish
```

Output: `evidence/upgrade-timing-rds.json`. Inventory, account identifiers, endpoint, credentials, raw errors and raw AWS event messages stay under ignored `.lab/`. The Aurora evidence is unchanged.
