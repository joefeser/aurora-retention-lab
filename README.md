# Aurora retention lab

A disposable proof of concept for archiving Aurora PostgreSQL messaging data to S3 before retiring it from operational storage.

The lab will evaluate range partitioning with `pg_partman`, partition export with `aws_s3`, related-table integrity, and migration from existing non-partitioned tables. The motivating workload has a 30-day operational retention requirement and a seven-year archive requirement, with a large historical backlog and expensive cascading deletes.

Only synthetic data belongs in this repository or the lab. This is an experiment, not a production migration or a compliance certification.

## Repository workflow

The initial `main` commit contains intent and scope documentation only. All implementation, SQL, infrastructure templates, AWS commands, and experimental results belong on the integration branch, `codex/retention-poc`, and reach `main` through a pull request. There is no `dev` branch.

## What we intend to establish

- Whether time partitioning can meet operational retention while preserving the required relationships and identity semantics.
- Whether archived exports can be verified and restored before a partition is retired.
- Which migration approaches are feasible for a large existing table, and what locking, copying, storage, and application changes each requires.
- How this compares with copying rows to a separate archive database.
- How to create and completely tear down an isolated AWS experiment.

See [the experiment scope](docs/experiment-scope.md) and [the lab lifecycle](docs/lab-lifecycle.md).
