# Aurora retention lab

A disposable proof of concept for archiving Aurora PostgreSQL messaging data to S3 before retiring it from operational storage.

The lab will evaluate range partitioning with `pg_partman`, partition export with `aws_s3`, related-table integrity, and migration from existing non-partitioned tables. The motivating workload has a 30-day operational retention requirement and a seven-year archive requirement, with a large historical backlog and expensive cascading deletes.

Only synthetic data belongs in this repository or the lab. This is an experiment, not a production migration or a compliance certification.

## Repository workflow

The initial `main` commit contains intent and scope documentation only. All implementation, SQL, infrastructure templates, AWS commands, and experimental results reach `main` through feature/integration branches and pull requests. The first phase used `codex/retention-poc`; phase 2 uses `codex/retention-scale`. There is no `dev` branch.

## What we intend to establish

- Whether time partitioning can meet operational retention while preserving the required relationships and identity semantics.
- Whether archived exports can be verified and restored before a partition is retired.
- Which migration approaches are feasible for a large existing table, and what locking, copying, storage, and application changes each requires.
- How this compares with copying rows to a separate archive database.
- How to create and completely tear down an isolated AWS experiment.

See [the experiment scope](docs/experiment-scope.md) and [the lab lifecycle](docs/lab-lifecycle.md).

## Run the POC

Start with `python3 scripts/run-local.py` (Python 3 and Docker required).
See [running and tearing down the lab](docs/running-the-lab.md) for the AWS experiment, commands, costs, and evidence limits.

Results: [observed findings](docs/findings.md) and [PostgreSQL 16/17 capability matrix](docs/version-matrix.md). Local tests passed on 16.15 and 17.11; the small S3 experiment passed on Aurora 17.9. These are correctness proofs, not production-scale benchmarks.

Phase 2: [load and concurrency results](docs/phase-2.md). Both local versions passed the cooperative migration rehearsal; Aurora 17.9 restored 128 MiB and 1 GiB datasets with exact payload matches. The results include commands, acceptance conditions, and limits on what the measurements establish.
