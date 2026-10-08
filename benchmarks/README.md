# Source-shaped retention experiments

These benchmarks implement the supplied sanitized parent/queue column types and nullability. They model the reported relationships and use deterministic synthetic content based on the supplied sizes and message descriptions. They do not contain production data or claim observed production distributions.

## Reproduce

```sh
python3 -m venv .lab/perf-venv
.lab/perf-venv/bin/pip install -r benchmarks/requirements.txt
.lab/perf-venv/bin/python benchmarks/run_workload.py --target postgres:17 --rows 4096 --trials 3
.lab/perf-venv/bin/python benchmarks/run_query_matrix.py --target postgres:17 --rows 2000000 --samples 30
.lab/perf-venv/bin/python benchmarks/run_migration_probes.py --target postgres:17 --rows 4096
.lab/perf-venv/bin/python benchmarks/run_dotnet.py --target postgres:17
.lab/perf-venv/bin/python benchmarks/run_fidelity_boundaries.py --target postgres:17
```

Repeat with `postgres:16` for the local version comparison. Docker images must already exist; the wrappers never pull them. Local databases have a random owned container name, a generated password, a loopback-only ephemeral port, two CPUs, a 4 GiB memory limit, and a 3 GiB temporary data filesystem. Each wrapper removes only its own container. Local WAL results describe this temporary PostgreSQL environment, not Aurora storage.

`run_workload.py --target aurora` and the query/migration/fidelity drivers use only the account/stack already recorded in ignored `.lab/aws.json`, with ownership checks and a maximum 1 ACU gate. They do not create infrastructure or make the private database public. Aurora calls use the Data API; this is not an EF/Npgsql-over-RDS-Proxy measurement. An existing AWS CLI login is required, including the locked CRT dependency for the SDK login provider.

Archive and retirement runs are deliberately **quiescent**. Exact synthetic arm schemas are cloned from one profile so all designs receive identical original values. Five logical relations are archived and restored before retirement; the local path uses actual binary COPY through bounded file staging, while Aurora uses CSV export/import through S3 with SSE-KMS checks. Every original column is compared with bidirectional `EXCEPT ALL`, including JSON, UUIDs, NULLs, empty text, and payloads. The template row is included explicitly, addressing the supplied legacy archive gap as a fixture choice.

After archive verification the transaction is committed. Retirement uses new transactions and there are no writers between them. This separation is safe only for this isolated benchmark; it is not an online archive controller. DELETE and payload-split identity cleanup use separately committed batches. The partition arm drops expired children before parents and explicitly cleans soft history. Copy-keepers rebuilds declared keys/FKs, advances identity sequences, swaps schema names, verifies survivors, and only then removes the old synthetic copy. Copy timing covers rebuild/swap and excludes later removal of the retained old schema. Storage-after covers the active schema, not all coexistence/scratch space. Work grants, extra triggers, RLS, and post-cutover rollback are not modeled.

The `split` mode is a historical aggregate-payload variant with one expired payload partition. `split_daily` uses 45 daily payload partitions, matching the parent's daily granularity in the composite-key arm. Do not conceal this DDL-count difference when comparing them. Current defaults use `split_daily`; both are available as explicit sensitivity cases.

Supporting child FK indexes are a declared benchmark assumption because the supplied material does not establish the complete index inventory. `--no-fk-index --modes delete` measures that sensitivity. `--batch-size 64|256|1024 --modes delete` measures batching while retaining the same input data. No queue uniqueness constraint is added; the supplied ORM/database mismatch is preserved.

## Dataset contract

`workload.py` is the canonical DDL/data generator; [the rendered baseline DDL](../docs/report/sanitized-schema.sql) is for inspection. Parent has all 17 supplied columns; queue has all 17. Identity generation and the four cascade FKs come from the subsequent supplied schema review. Push/history/template payload definitions were not supplied, so those use minimal synthetic columns and are labeled as approximations.

- Typical parent HTML cycles through 5/10/20/30/50 KiB. Queue HTML cycles through 3/4/6/8 KiB.
- Tail profile assigns 1% of parent IDs a 1 MiB body and an additional approximately 4% a 200 KiB body, before deterministic NULL/empty rules. These frequencies are stress assumptions, not work measurements.
- Skew profile keeps tail payloads and gives approximately 5% of parents 40 push rows, with two for other parents. Typical/tail use three push rows per parent.
- One queue per parent, soft history for one in four queues, about 80% expired parents, 45 expired daily buckets and one broad future bucket are explicit assumptions. This is a backlog-shaped retirement, not a steady-state single-day retirement with 30–40 remaining daily leaves.
- The logical observation date is October 31, 2026 and the fixed retention cutoff is October 1. It is independent of the computer's current clock. Queue scheduling may diverge from parent time. Source retention stays parent-aligned in the partition option.
- Measured payload distributions, age buckets, fanout, column metadata, and physical size are retained in receipts. HTML contains repeated markup plus deterministic per-row tokens; it is neither all random hex nor a claimed sample of work's HTML compression ratio.

## Measurement boundaries

`run_workload.py` records server retirement time, full client retirement time, individual commit latency, archive/export/import/verification timing, storage before/after, and local cluster-wide WAL deltas when available. The Aurora WAL functions tested are unavailable, so those fields remain null. WAL deltas include other database activity; isolated local fixtures reduce but do not eliminate background activity. They are not Aurora billed write I/O. All batches commit, and three rotated/reversed trials are retained without hiding slow trials. Report medians/ranges for these small trial counts, not request p95 claims.

The two-million-row query matrix deliberately uses only ID, timestamp, and UUID columns to isolate partition planning/index sensitivity. It is not a two-million-row HTML workload. It uses five warmups and 30 measured lookups per query shape, retains a representative JSON EXPLAIN with buffers and WAL, and separates direct timestamp constants from runtime timestamp expressions. Its nearest-rank p95 is a small-sample observation.

The BenchmarkDotNet project maps all parent/queue fields and the reported navigations. Canonical SQL creates the schema; the EF model is a read/update client and does not call EnsureCreated or generate a migration that would add a queue uniqueness constraint. It compares full parent reads, external-reference reads, joined/split graph loads, and rolled-back tracked rescheduling with auto-preparation on/off, against ID-only and composite schemas. It uses Release .NET 10, one launch, three warmup iterations, ten measured iterations, and 100 operations per invocation. Mean/stddev and allocations are per operation; percentiles of iteration averages must not be presented as individual-request tail latency. The source-shaped tail profile and the same 100 identities are used in every case. Complete table contents are compared afterward to verify mutations rolled back.

These experiments do not certify production throughput, a 100M-row completion time, replica lag, vacuum cost, long-term compliance, Aurora failover, proxy behavior, or a lossless production cutover. The full report distinguishes measured results, documented behavior, supplied observations, and remaining decisions.

## Report and verification

`python3 benchmarks/publish_report.py` verifies the committed receipt hashes and regenerates all Markdown/CSV performance tables from the manifest. Successful receipts are stored as deterministic gzip JSON to keep complete plans and trial detail compact. The report excludes smoke runs and superseded query/EF variants explicitly. There is no inference from a missing run to success.

```sh
python3 -m unittest discover -s tests
.lab/perf-venv/bin/python benchmarks/test_harness.py
# Optional real SQL ownership/recovery test; creates and removes its own PG17 container.
.lab/perf-venv/bin/python benchmarks/check_recovery_local.py
dotnet restore samples/Retention.Workload --locked-mode
dotnet build samples/Retention.Workload -c Release --no-restore
python3 benchmarks/publish_report.py
```

The larger full-schema scale sensitivity uses `--rows 32768 --profiles typical --trials 3` on local PostgreSQL 17 under the same resource limits; the earlier tail sensitivity uses 16,384 parents. These remain distinct from the two-million-row narrow-key query matrix. `observe_cloud.py` optionally records read-only CloudWatch context over four hours; its aggregate window cannot attribute I/O or CPU to an individual arm.

The optional static plots use `plot-requirements.txt` in a separate environment, then `python benchmarks/plot_report.py`. Plot values come exclusively from the generated CSV tables; SVG and PNG outputs are committed with the report.

## Publication contract and failed-run recovery

Public receipts use the explicit version 1 field contract in
[`receipt-fields-v1.json`](receipt-fields-v1.json), with numeric/coverage checks in
`receipt_contract.py` and `publish_report.py`. The field grammar is a reviewed,
committed allowlist of object paths; it is not learned from incoming receipts.
New collector fields or EXPLAIN node shapes require an explicit contract update.
This rejects unexpected metadata and credential-shaped keys, but does not replace
human sanitization of arbitrary text in otherwise approved fields.

Publication first verifies and derives the entire selection in a temporary directory.
Unsupported manifest versions, malformed BDN statistics/Memory, unapproved fields,
and conversion failures leave public outputs untouched. An interrupted final file
replacement can leave files partially replaced, but the manifest is invalidated first
and written last; plots fail closed without that binding. Re-run from the original
selection after such an interruption. Empty tables from omitted runs are removed.
A changed selection invalidates old generated plots. The plotter verifies table hashes,
evidence labels and the complete configurations described by its captions.
Narrative documents are authored separately and must be revised before publishing a
subset as a new study.

CloudWatch keeps valid datapoints when a metric has malformed entries or an API
failure, continues collecting subsequent metrics, and marks the receipt incomplete.
Incomplete observations cannot support publication. `cloud-observation.csv` contains
window summaries; `cloud-datapoints.csv` contains each timestamp, unit and period's
minimum/average/maximum.

New Aurora benchmark runs write an ignored `.lab/recovery-<token>.json` inventory
**before** schema or S3 side effects. Exact schema names carry a run ownership comment
created atomically with the schema; rename destinations are also inventoried.
Archives use one random prefix per run, including any unexpected export suffixes.
The inventory is bound to the checked lab's cluster and bucket without recording their
identifiers. Keep the local inventory until cleanup is confirmed. Existing historical
runs predate this inventory and remain covered by whole-lab teardown.

`Database.close()` still rolls back and preserves the original failure. It prints the
inventory path and records retained-resource status; it does not delete archives while
an unacknowledged export might still be running. After stopping the runner and checking
that in-flight SQL/exports have finished, inspect and recover that exact inventory:

```sh
.lab/perf-venv/bin/python benchmarks/cloud_recovery.py .lab/recovery-<token>.json
.lab/perf-venv/bin/python benchmarks/cloud_recovery.py .lab/recovery-<token>.json --execute --confirm-run-stopped
```

Recovery rechecks the lab binding, refuses to drop schemas without the matching marker,
and lists/deletes only that inventory's exact archive prefix. It has a two-minute
between-operation budget, per-statement/lock limits, bounded SDK calls and a ten-batch
S3 ceiling. A call already in flight can finish after the between-operation deadline.
Cleanup failures retain typed results in the inventory without overwriting experiment
failure evidence; rerun after resolving the cause. A versioned bucket is refused rather
than treating delete markers as removal of archive bytes. Successful-run archives remain
available until explicit recovery or the scheduled whole-lab teardown.

The ownership COMMENT adds one DDL statement to future Aurora schema creation,
including copy-keepers timing. Published historical measurements are unchanged and
predate this recovery instrumentation; new timings should not be represented as exact
replays of those historical bytes.
