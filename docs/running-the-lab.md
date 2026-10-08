# Running the synthetic lab

## Local PostgreSQL evidence

Requirements: Python 3 and Docker. From the repository root:

```sh
python3 scripts/run-local.py
python3 scripts/run-local.py --image postgres:16
```

PostgreSQL 17 is the initial AWS target because the supplied research claimed version-dependent capabilities; the production version is not yet confirmed. Both local PostgreSQL 16 and 17 are tested. Docker resolves the image tag; each run records the actual image ID and SQL engine version. Each run creates a uniquely named, network-isolated container with no published ports, a 512 MB temporary database filesystem, a 1 GB memory limit, and two CPUs. It removes only that container and its volumes in a `finally` block. Logs remain under ignored `.lab/`.

The local fixture proves 19 properties with actual SQL and expected SQLSTATEs. It covers composite keys, identity inserts, one-to-one enforcement, mutable delivery timestamps, dependency rejection, DDL versus row cascades, local CSV fidelity, a copy-keepers rehearsal, and the claimed SPLIT PARTITION syntax. Synthetic fixed dates make these checks repeatable; they are not a live retention policy.

The migration rehearsal freezes all modeled writers for the entire operation. It copies all related keepers, rebuilds their foreign keys, advances the new identity sequence, swaps names, and demonstrates why the old copy becomes stale after writes resume. The whole rehearsal rolls back. It neither archives the discarded set nor authorizes a production migration.

## AWS lab

Requirements: AWS CLI with an authenticated session, Python 3, CloudFormation/IAM/RDS/S3 permissions. No database password is written locally. AWS manages the secret; the Data API receives its ARN.

```sh
python3 scripts/aws-lab.py create --account YOUR_ACCOUNT_ID --region us-east-2 --engine-version 17.9
python3 scripts/aws-lab.py status
# When status is CREATE_COMPLETE (or UPDATE_COMPLETE after a template update):
python3 scripts/run-aws.py
```

The infrastructure is defined in `infra/lab.json`: dedicated VPC, two private subnets, an S3 gateway endpoint, no inbound database access, one encrypted Aurora Serverless v2 writer, a managed secret, separate import/export roles, and a private SSE-KMS bucket using the AWS-managed S3 key. It includes no NAT gateway, public database, EC2 host, RDS Proxy, Object Lock, or retained final snapshot. All resources are disposable and intended for synthetic data only.

The AWS experiment uses 100 expired parent/queue pairs plus a live pair. Payloads include NULL, empty strings, quoted text, embedded newlines, Unicode, and a roughly 1 MB value. It tests a failed export, exports each related partition, imports both objects, compares every value with `EXCEPT ALL` in both directions, detects a deliberately corrupted same-count restore, writes a verified-export manifest, and retires child then parent while preserving the live pair. It also proves manual `pg_partman` maintenance with automatic retention disabled.

The export/verification/retirement proof runs under exclusive locks in one database transaction. This is deliberately a tiny correctness experiment, not a production concurrency design. S3 writes are not transactional: a failed run can leave uniquely prefixed objects; retry creates a fresh schema and prefix, and teardown empties the dedicated bucket. Evidence and account-specific inventory stay in `.lab/`. The manifest records export verification, not a claim of committed retirement. The final local receipt records whether commit succeeded.

## Cost and teardown

Capacity is bounded to 0.5–1 ACU. The AWS pricing API returned USD 0.12 per ACU-hour for Aurora PostgreSQL Serverless v2 in Ohio during setup: at most USD 2.88/day in compute at this setting. Storage, I/O, S3, secret, and applicable tax charges are additional. This small fixture is expected to remain comfortably within the USD 50 total budget through the planned teardown, but a capacity cap and reminder are not a billing hard stop. Do not run unbounded loads.

```sh
python3 scripts/aws-lab.py delete
python3 scripts/aws-lab.py status
```

Wait for `DELETE_COMPLETE`. The deletion command verifies the authenticated account, recorded stack identity, and project tag, then empties only buckets belonging to that stack and requests stack deletion. It stops if unexpected versioning is enabled. If deletion fails, inspect CloudFormation events and fix the specific remaining lab resource; do not broaden cleanup to unrelated resources. RDS-managed secrets are tied to the cluster lifecycle. Confirm no lab DB instances/clusters, buckets, or snapshots remain before reporting teardown complete.

## Explicitly unproved

- Production throughput, WAL volume, costs at terabyte scale, and migration duration.
- RDS Proxy, EF Core integration, external writers, and concurrent cutover/rollback.
- Seven-year durability/compliance, schema evolution, archive querying, or large multi-object exports.
- Automated scheduled maintenance and recovery from uncertain transaction outcomes.
- Production retirement policy, event/history retention, and payload/identity split performance.

The local fixture models event and soft history dependencies. The AWS archive fixture currently models only parent and queue; that smaller proof is not acceptance of production retirement.

## References

- [PostgreSQL partitioning](https://www.postgresql.org/docs/17/ddl-partitioning.html)
- [Aurora S3 export](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/postgresql-s3-export-functions.html)
- [Aurora S3 import](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/USER_PostgreSQL.S3Import.FileFormats.html)
- [Aurora pg_partman maintenance](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/PostgreSQL_Partitions.html)
