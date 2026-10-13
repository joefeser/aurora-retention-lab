# Opus prompt: assess whether the PostgreSQL upgrade plan will work

Copy the prompt below into the team's Opus session. It contains only public, synthetic lab context. Keep any internal environment comparison inside the team's approved workspace.

---

You are reviewing an evidence-backed database experiment for an engineering team. Help us answer: **“Will an in-place PostgreSQL 16 → 17 upgrade work for our environment, and can we reasonably plan a maintenance window of one hour or less?”** Give an independent assessment, not a restatement of the lab's conclusions. Challenge unsupported claims and distinguish demonstrated behavior from assumptions.

## Evidence to inspect

Public repository: https://github.com/joefeser/aurora-retention-lab

Use this exact evidence snapshot, even if the branch or PR changes:

`c76364dcc3e94e44a23d16b155b1f97f6e931312`

- Findings and methods: https://github.com/joefeser/aurora-retention-lab/blob/c76364dcc3e94e44a23d16b155b1f97f6e931312/docs/upgrade-timing.md
- Raw public receipts: https://github.com/joefeser/aurora-retention-lab/blob/c76364dcc3e94e44a23d16b155b1f97f6e931312/evidence/upgrade-timing.json
- Runner: `scripts/run-upgrade-timing.py`
- Synthetic schema and generator: `benchmarks/workload.py`
- Infrastructure: `infra/lab.json`
- Regression checks: `tests/test_upgrade_timing.py`
- Related retention study, only when relevant to the team's broader design: `docs/report/full-report.md`, `docs/version-matrix.md`, and `docs/study-review-guide.md`.

PR #11 is the review context: https://github.com/joefeser/aurora-retention-lab/pull/11. Do not assume that an open PR or a passing test suite establishes production readiness. Verify the pinned artifacts directly. If you cannot access them, say which are unavailable and request those files; do not imply you inspected them.

## What was tested

This was **Aurora PostgreSQL**, not a standard Amazon RDS for PostgreSQL instance. Each case used a fresh, private, single-writer Aurora cluster in us-east-2. The upgrade path was **16.15 → 17.11**, with no application traffic, production-specific parameter group, application extensions, or partition DDL in the upgrade window.

All data was synthetic, using the existing `typical` fixture. Five relations were loaded: `notification_event`, `delivery_queue`, `push_delivery`, `send_history`, and `notification_template`.

| Tier | Parent rows | Queue rows | Push rows | History rows | Template rows | Measured database size, approximately |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 1x | 32,768 | 32,768 | 98,304 | 8,192 | 1 | 0.422 GiB |
| 4x | 131,072 | 131,072 | 393,216 | 32,768 | 1 | 1.668 GiB |
| 16x | 524,288 | 524,288 | 1,572,864 | 131,072 | 1 | 6.685 GiB |

The byte measurements include normal PostgreSQL storage, indexes and TOAST behavior. Do not substitute logical payload size, row counts, or an assumed uncompressed size for the recorded physical bytes.

The original Serverless v2 **0.5–2 ACU / 1x** attempt failed inside AWS-managed `pg_upgrade`, then rolled back to 16.15. The CLI accepted the request with exit 0 and empty stderr; the failure occurred later. The preserved service diagnostic reported requested shared memory of **6,330,675,200 bytes** against **5,158,047,744 bytes** of system memory. All five counts matched after rollback. This does not establish the underlying AWS root cause or the minimum capacity needed. The failed attempt was not retried automatically. Its manual reconciliation and original execution provenance are disclosed in the report.

The owner then authorized fixed **4 ACU minimum and maximum**, plus the provisioned comparison. All six follow-up cases passed:

| Configuration | 1x upgrade seconds | 4x upgrade seconds | 16x upgrade seconds |
| --- | ---: | ---: | ---: |
| Serverless v2, fixed 4 ACU | 481.63 | 541.99 | 451.41 |
| Provisioned `db.r6g.large` | 451.49 | 482.14 | 481.71 |

All five table counts and measured table total/heap/index sizes matched before and after each successful upgrade. Database-wide size changed slightly. **No payload checksum comparison or application functional validation was performed.** Each case has one observation, with approximately 30-second polling. The two larger original 0.5–2 ACU cases remain intentionally unmeasured.

Timing starts immediately before the CLI upgrade request and ends when the cluster and writer report available at the exact target engine version and SQL responds from PostgreSQL 17. Loading, subsequent table checks, cleanup, application validation, and post-upgrade statistics work are not all included in that number. Every case recorded failed Data API SQL probes; the successful cases' sampled interruption upper envelopes were roughly 7–8 minutes. **Data API availability is not a direct TCP/Npgsql/application downtime measurement.**

Capacity was configured at creation. We did not test increasing capacity on an existing cluster immediately before upgrade or reducing it afterward. All seven attempts' stacks, clusters, writers, managed secrets and snapshots were verified removed. The servers are off. This handoff authorizes analysis only; do not provision, modify, or restart AWS resources. Any further live test requires a new owner instruction and a refreshed budget.

## Analysis requested

1. **Audit the evidence.** Recompute the timing/size summary from the JSON; verify the six-case coverage, source/target SQL and control-plane versions, all five counts, and failure classification. Inspect the probe sequences and what the timer actually includes. Each receipt records its execution `git_head` and source SHA-256 values; compare against those historical files, not only the final runner. Distinguish later reporting fixes from code used during execution. Local tests and hash consistency do not independently authenticate historical cloud timing.

2. **Compare with our actual environment.** Use the sanitized schemas/use cases and approved internal context already available to you. Do not assume the lab's topology or sizes match ours. Explicitly establish Aurora versus standard RDS PostgreSQL; exact versions; current capacity and topology/readers; measured physical sizes and object counts; extensions and parameter groups; partition/key/FK model; replication/CDC dependencies; concurrent load; connection pools and retry behavior; and critical application queries. Mark missing facts as unknown. Explain which differences could invalidate transferring these results.

3. **Answer the three decision questions separately.**
   - Is the engine upgrade technically feasible for our actual setup?
   - Is a ≤1 hour *total service maintenance window* supported, conditionally plausible, or not yet supported?
   - Is the wider retention/partitioning design viable? Treat this separately from engine-upgrade timing. A successful upgrade does not prove partition operations, preserve an ID-only PK/FK design under timestamp partitioning, or validate production migrations. If partition features matter, inspect the separate capability evidence and current official documentation rather than assuming version 17 solves them.

4. **Assess capacity and availability honestly.** Does the failure justify a temporary capacity increase for our setup? What does fixed 4 ACU actually demonstrate? Do not label it a proven minimum, universal fix, or tested live-resize procedure. Do not infer a consistent performance advantage or a size-to-duration formula from one run per case. Explain what the observed SQL interruption means for application connections and what remains unmeasured.

5. **Build a maintenance-window budget.** Separate prechecks, capacity adjustment/reboot if needed, traffic drain, managed upgrade, reconnection, statistics refresh, functional/query validation, and release of traffic. Identify activities that can occur before the outage. Use measured numbers only for the measured stage; label remaining durations unknown or explicit assumptions. Do not call the one-hour target proven if these stages have not been bounded. Explain rollback versus restore/recovery and distinguish the lab's automatic failed-upgrade rollback from a tested recovery plan after a successful major upgrade. Verify relevant current AWS/PostgreSQL rules in official sources and cite them.

6. **Recommend only tests that could change the decision.** If more evidence is necessary, propose the smallest prioritized next test set, with the uncertainty addressed, representative setup/data, acceptance signal, expected duration/cost assumptions, stop conditions, and teardown plan. Candidates might include application/TCP interruption and reconnection, representative extensions/parameters, critical query plans and statistics, a repeat run for variability, capacity transition, or recovery rehearsal. Choose based on actual gaps; do not turn this into an unrestricted benchmark program or run anything now.

## Return format

- A short executive recommendation: **supported for the tested fixture / conditionally supported for our environment / not yet demonstrated / contradicted**, with a separate verdict for technical upgrade feasibility, the one-hour total window, and the wider retention design.
- A decision matrix: claim, evidence reference, demonstrated/inferred/unknown status, environment mismatch, and what would change the decision.
- A concrete maintenance-window outline with measured versus assumed durations clearly distinguished.
- Ranked blockers and the smallest further test plan, if needed. Separate mandatory blockers from optional confidence improvements.
- A concise message the team can use in its decision discussion, avoiding “fully proven,” “zero downtime,” or “production ready” unless the necessary evidence truly exists.

Cite repository paths, receipt run IDs and exact fields for factual claims, plus current official documentation where needed. Keep the response and any shareable artifacts sanitized: no account IDs, credentials, endpoints, private paths, customer data, or company-specific identifiers. Do not upload private source documents into the public repository. If your conclusion differs from the lab report, explain the discrepancy with specific evidence.
