# Phase 3: archive-verified retirement comparison

This phase compares two synthetic retention designs on the existing Aurora 17.9 lab. Both archive the same expired relationship set, verify SSE-KMS, restore it, and compare every column with `EXCEPT ALL` in both directions before retiring anything. Results are bounded samples, not a production sizing model.

## Fixture and method

```sh
python3 -m unittest discover -s tests -v
python3 scripts/run-retirement.py
```

Each arm contains 1,024 expired parents and 32 live parents. Every parent has a 32 KiB deterministic body made from distinct MD5 blocks, one queue with an 8 KiB body, two push rows, and one soft-history row. Expired parent and queue bodies total 40 MiB per arm. Payloads and IDs are identical between arms. This artificial hex text does not model production HTML compressibility.

The baseline uses ID-only primary keys and parent foreign keys with `ON DELETE CASCADE`. The queue relationship has a unique index, push foreign keys have an index, and soft history has a queue-ID index. It explicitly deletes history joined through queue and parent before deleting parents; history has no FK and would otherwise remain behind.

The partition design uses composite keys including the parent's retention timestamp, with aligned daily partitions on all four tables. It drops expired history, push, and queue partitions, then detaches and drops the expired parent partition. Drops use the default restrictive behavior. One expired queue's delivery time is deliberately moved into the live period in both models; retention still follows its parent's time.

The runner creates a fresh schema for each arm. Trial 1 runs DELETE then partition retirement; trial 2 reverses the order. Both paths acquire access-exclusive locks on all four tables and hold them through export, restoration, validation, retirement, keeper checks, and commit. This intentionally quiescent method is a correctness fixture, not an acceptable production locking plan. It has no concurrent writers and does not measure contention.

Before destructive SQL, a guard requires verified archives for all four relations and an ignored local receipt is written. Source and restored contents are compared exactly, including duplicates. After retirement, all surviving columns must match saved keeper snapshots and counts must match in fresh requests after commit. A failed transaction rolls back database retirement; external S3 objects are not transactional and remain for inspection. Seeded source schemas also remain. A new invocation uses fresh names.

## Timing definitions

- `retire_server_seconds`: database wall time around explicit history cleanup plus parent DELETE, or child-first DROP plus parent DETACH/DROP. Excludes archive, lock acquisition, verification, and commit. Includes applicable statement/trigger/DDL work inside that operation.
- `retire_client_seconds`: that same operation as observed through the CLI and Data API, including transport overhead.
- `archive_verify_client_seconds`: keeper snapshots, exports, metadata requests, imports, and full comparisons.
- `lock_through_commit_client_seconds`: acquisition through the transaction's commit, including archive and verification. This is the relevant blocking interval for the fixture, not just the DROP time.

These are two sequential samples per design, with different physical layouts and changing cache/capacity conditions. They do not establish percentiles, WAL volume, vacuum cost, storage reclamation latency, sustained throughput, or a 100-million-row completion estimate. DELETE's later vacuum work is not timed. No speedup claim should be extrapolated from these fixtures.

## Observations

All four arms passed on October 8, 2026. The existing Aurora 17.9 cluster remained capped at 1 ACU. Every one of the 16 exported objects passed SSE-KMS and full restore comparison. Five Python unit tests passed, including rejection of missing history, wrong encryption, unverified content, and wrong row counts at the retirement guard.

| Trial | Design | Retirement, server | Retirement, client | Archive and verification | Lock through commit |
| --- | --- | ---: | ---: | ---: | ---: |
| 1 | delete | 0.119960 s | 0.584 s | 15.642 s | 19.532 s |
| 1 | partition | 0.182231 s | 0.657 s | 15.260 s | 19.592 s |
| 2 | partition | 0.021803 s | 0.418 s | 16.863 s | 20.622 s |
| 2 | delete | 0.080282 s | 0.543 s | 15.261 s | 19.199 s |

The run completed in 159.115 seconds including data generation and setup. Public evidence is in `evidence/retirement.json`. Additional fresh requests verified exact surviving keeper contents, both validated cascade FKs still referencing the active parent, and identical restored datasets across all four arms. All expired partition relations were absent after partition retirement.

The small DELETE samples took 0.080–0.120 seconds of server time; partition retirement took 0.022–0.182 seconds. These samples do **not** demonstrate a consistent speed advantage. Archive/restore verification took 15–17 seconds per arm and the complete blocking interval was roughly 20 seconds. The useful result is integrity across both strategies, plus visibility into costs that a DROP-only timing would hide.

## Limits and next decision

This experiment assumes parent-aligned retention is valid for every modeled child and explicitly treats soft history as expiring with its parent. Actual history retention, all incoming FKs, triggers, workload distribution, and independent child lifecycles need an application review. Partitioning requires schema/backfill and application/EF key changes; it cannot be added transparently to the current ID-only relationships. This runner does not implement that migration.

A production decision still needs representative workloads, concurrent writer/reader behavior, archive manifests and recovery policy, application mapping tests, and an agreed cutover/rollback procedure. Aurora 16 performance is not measured; the local 16/17 compatibility matrix remains separate from these Aurora 17.9 results.

The runner checks the recorded stack identity and live 1 ACU maximum. The 30-minute guard is checked before database requests; it is not cancellation of a request or a billing cutoff. The retirement transaction uses a 2-second lock timeout and 35-second statement timeout. No infrastructure is added. The original USD 50 budget and October 9 teardown reminder remain in effect. Run `python3 scripts/aws-lab.py delete` and verify `DELETE_COMPLETE` when finished.
