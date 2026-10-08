# Aurora retention lab

A disposable proof of concept for archiving Aurora PostgreSQL messaging data to S3 before retiring it from operational storage.

The repository records a completed, bounded synthetic study of range partitioning with `pg_partman`, partition export with `aws_s3`, related-table integrity, and migration from existing non-partitioned tables. The motivating workload has a 30-day operational retention requirement and a seven-year archive requirement, with a large historical backlog and expensive cascading deletes.

Only synthetic data belongs in this repository or the lab. This is an experiment, not a production migration or a compliance certification.

## Start here: humans and AI reviewers

1. Read the [full study and conclusions](docs/report/full-report.md), including its acceptance matrix, assumptions and limits.
2. Use the [review and validation guide](docs/study-review-guide.md) to trace a claim from the report to CSV rows, raw receipts and the code that produced it.
3. Inspect the [complete performance tables](docs/report/performance-tables.md) and [receipt manifest](evidence/workload-report/manifest.json). The manifest identifies 23 successful runs: 168 retirement trials, 75 query cases, 48 EF benchmark cases, plus migration and archival boundary evidence.
4. Read the [PostgreSQL 16/17 capability matrix](docs/version-matrix.md) for version claims, and the [benchmark methods](benchmarks/README.md) before rerunning experiments.

The study is complete for the recorded fixtures, not a production readiness certification. It does not prove a 100M-row completion time or a 10× payload-split improvement. The two-million-row tests use a narrow key projection; the full-schema datasets are smaller. Original work attachments and real data are not included: deterministic synthetic data can be regenerated from the supplied schema shapes and declared assumptions.

### Validate the published study without AWS or Docker

From the repository root, using Python 3:

```sh
python3 -m unittest discover -s tests
python3 benchmarks/publish_report.py
git diff --exit-code -- docs/report evidence/workload-report
```

The first command tests validation logic; the second verifies compressed/uncompressed receipt hashes and rebuilds the tables. The last detects changes relative to the checked-out commit. This checks the published evidence and computations; it does **not** rerun PostgreSQL, authenticate the historical execution, or independently reproduce timings. Work in a clean checkout so unrelated edits do not obscure the comparison. See the guide for raw-receipt inspection, independent median recomputation, local experiment replay and optional chart generation.

## Repository workflow

Implementation, scripts, infrastructure and evidence reach `main` through feature/integration PRs and human review. There is no `dev` branch. The initial `main` commit contained intent and scope documentation only. For the current experiment window and teardown procedure, use the [canonical lab lifecycle](docs/lab-lifecycle.md).

## Questions investigated

- Whether time partitioning can meet operational retention while preserving the required relationships and identity semantics.
- Whether archived exports can be verified and restored before a partition is retired.
- Which migration approaches are feasible for a large existing table, and what locking, copying, storage, and application changes each requires.
- How this compares with copying rows to a separate archive database.
- How to create and completely tear down an isolated AWS experiment.

See [the experiment scope](docs/experiment-scope.md) and [the lab lifecycle](docs/lab-lifecycle.md).

## Earlier phases and correctness proofs

Start with `python3 scripts/run-local.py` (Python 3 and Docker required).
See [running and tearing down the lab](docs/running-the-lab.md) for the AWS experiment, commands, costs, and evidence limits.

Results: [observed findings](docs/findings.md) and [PostgreSQL 16/17 capability matrix](docs/version-matrix.md). Local tests passed on 16.15 and 17.11; the small S3 experiment passed on Aurora 17.9. These are correctness proofs, not production-scale benchmarks.

Phase 2: [load and concurrency results](docs/phase-2.md). Both local versions passed the cooperative migration rehearsal; Aurora 17.9 restored 128 MiB and 1 GiB datasets with exact payload matches. The results include commands, acceptance conditions, and limits on what the measurements establish.

Phase 3: [archive-verified retirement comparison](docs/phase-3.md), comparing indexed cascading DELETE plus soft-history cleanup with coordinated partition retirement.

Phase 4: [archival with concurrent live inserts and reads](docs/phase-4.md), testing expired-leaf write fencing and brief parent locks for retirement.

Phase 5: [query shapes and bounded recovery](docs/phase-5.md), covering abandoned exports, stale archives, reader contention, and transactional DDL rollback.

Phase 6: [larger synthetic case and decision report](docs/phase-6.md), including remaining production gates and a [sanitized work-side validation request](docs/work-validation-request.md).

Phase 7: [prepared-plan admission](docs/phase-7.md), showing why partition-qualified prepared queries still need plan-cache compatibility tests.

Phase 8: [EF application sample and compatibility results](docs/phase-8.md), built from the supplied sanitized relationship model, with local PostgreSQL 16/17 migration, plan reuse, cascade, and archive-retirement probes.

Phase 9: [source-shaped performance study](docs/report/full-report.md), with all 17 parent and 17 queue columns, full-row EF/BenchmarkDotNet, large narrow-key query matrices, archive round trips, migration probes, and [complete performance tables](docs/report/performance-tables.md).
