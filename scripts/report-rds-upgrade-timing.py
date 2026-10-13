#!/usr/bin/env python3
"""Derive the RDS report and CSV offline from sanitized, validated receipts."""
import csv
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('rds_report_source', ROOT / 'scripts/run-rds-upgrade-timing.py')
u = importlib.util.module_from_spec(spec)
spec.loader.exec_module(u)
MEMORY = {'db.r7g.xlarge': 32, 'db.r7g.large': 16}
CPU = {'db.r7g.xlarge': 4, 'db.r7g.large': 2}
PLANNED = {('small', 'db.r7g.xlarge'), ('medium', 'db.r7g.xlarge'),
           ('large', 'db.r7g.xlarge'), ('medium', 'db.r7g.large')}


def clock(seconds):
    if seconds is None:
        return 'unbounded'
    rounded = round(seconds)
    return f'{rounded // 60}:{rounded % 60:02}'


def main():
    raw = json.loads((ROOT / 'evidence/upgrade-timing-rds.json').read_text())
    cases = [u.public_receipt(c) for c in raw['cases']]
    keys = [(c['tier'], c['instance_class']) for c in cases]
    if len(set(keys)) != len(keys) or not set(keys) <= PLANNED:
        raise ValueError('Report expects one trial per planned tier/class')
    prices = json.loads((ROOT / 'evidence/rds-instance-pricing.json').read_text())
    cases.sort(key=lambda c: (c['instance_class'] != 'db.r7g.xlarge', u.TIERS[c['tier']]))
    rows = []
    for c in cases:
        episodes = c['interruption']['episodes']
        upper = max((e['upper_seconds'] for e in episodes), default=None) if all(e['upper_seconds'] is not None for e in episodes) else None
        rate = prices['instance_prices_usd_per_hour'][c['instance_class']] + c['allocated_storage_gib'] * prices['gp3_storage_usd_per_gb_month'] / prices['estimate_month_hours']
        rows.append({'tier': c['tier'], 'instance_class': c['instance_class'], 'memory_gib': MEMORY[c['instance_class']],
            'vcpu': CPU[c['instance_class']], 'parents': c['parents'], 'database_gib': c['before']['database_bytes'] / 2**30,
            'provision_seconds': c['provision_seconds'], 'load_seconds': c['load_seconds'],
            'request_to_ready_seconds': c['request_to_ready_seconds'], 'sql_episode_upper_seconds': upper,
            'sql_episode_count': len(episodes), 'validation_seconds': c['validation_seconds'],
            'total_seconds': c['total_seconds'], 'elapsed_compute_storage_estimate_usd': rate * c['total_seconds'] / 3600,
            'execution_git_head': c['provenance']['git_head']})
    with (ROOT / 'docs/report/rds-upgrade-timing.csv').open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    table = ['| Fixture | Class / RAM / vCPU | Database GiB | Load | Request → ready | Largest SQL interruption upper bound | Total incl. setup/teardown |',
             '|---|---|---:|---:|---:|---:|---:|']
    table.extend(f"| {r['tier']} | {r['instance_class']} / {r['memory_gib']} / {r['vcpu']} | {r['database_gib']:.3f} | {clock(r['load_seconds'])} | {clock(r['request_to_ready_seconds'])} | {clock(r['sql_episode_upper_seconds'])} | {clock(r['total_seconds'])} |" for r in rows)
    counts = ['| Fixture / RAM | Parents | Queue | Push children | History | Templates |', '|---|---:|---:|---:|---:|---:|']
    for c in cases:
        t = c['before']['tables']
        counts.append(f"| {c['tier']} / {MEMORY[c['instance_class']]} GiB | {t['notification_event']['rows']:,} | {t['delivery_queue']['rows']:,} | {t['push_delivery']['rows']:,} | {t['send_history']['rows']:,} | {t['notification_template']['rows']:,} |")
    sizes_equal = sum(all(all(c['before']['tables'][t][k] == c['after']['tables'][t][k]
        for k in ('heap_bytes', 'index_bytes', 'total_bytes')) for t in u.fixture.w.TABLES) for c in cases)
    total_estimate = sum(r['elapsed_compute_storage_estimate_usd'] for r in rows)
    unmeasured = sorted(PLANNED - set(keys))
    status = f'{len(cases)}/4 planned synthetic cases completed and cleaned.'
    if unmeasured:
        status += ' Unmeasured: ' + ', '.join(f'{tier} on {kind}' for tier, kind in unmeasured) + '.'
    provenance = '\n'.join(f"- {c['tier']} / {c['instance_class']}: `{c['provenance']['git_head']}`" for c in cases)
    text = f'''# Standard RDS PostgreSQL 16 → 17 timing results

{status} These are standard RDS (`Engine=postgres`) measurements; the separate Aurora receipts are not used in this report.

## Measurements

One trial per case, PostgreSQL **16.15 → 17.11**, Ohio (`us-east-2`), Single-AZ, encrypted **400 GiB gp3**, observed **12,000 IOPS / 500 MiB/s**. The instance classes use Graviton3. Times are minutes:seconds, rounded for readability. Allocated storage and measured database bytes are different quantities.

{chr(10).join(table)}

Request-to-readiness ends only after AWS reports the target version available and a fresh PostgreSQL connection has returned the target version. SQL interruption is a last-success to first-success observation envelope, not an exact outage duration. The full receipts retain every episode; the table shows the largest bound if there are multiple episodes. No failed sample would not prove zero downtime between probes.

[Full sanitized receipts](../evidence/upgrade-timing-rds.json) contain all probes, AWS status samples, five-table byte measurements, constraints, sampled content hashes, configuration and code hashes. [CSV](report/rds-upgrade-timing.csv) retains unrounded values. [Service-log observations](../evidence/rds-upgrade-log-observations.json) retain safe phase labels and raw-capture hashes; raw logs remain private.

## What the experiment tests

The small, medium and large fixtures retain the same five-table schema, indexes, constraints and data-generation rules while increasing parent counts. This tests size sensitivity for a fixed object shape. It does not vary catalog complexity, database count, extensions, partition count, replication or concurrent application activity. The 16 GiB comparison changes both RAM and vCPU count; it cannot isolate a RAM-only effect.

The RDS service logs captured for the initial cases report `Linking user relation files` and `Upgrade Complete`. This supports AWS's documented use of `pg_upgrade --link`, which avoids a full data-file copy during that step. It does not mean snapshots, preparation, restart, managed recovery or subsequent statistics work take no time. [AWS object-count guidance](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/PostgreSQL.HighObjectCount.html).

A proportional stored-bytes model is therefore not a justified way to predict production downtime. These measurements can challenge that model for this fixture, but one trial per case cannot establish a reliable slope, variance or confidence interval. A rehearsal of the actual environment is still needed for an operational maintenance window.

## Data checks and cleanup

Every published case preserved expected relation counts, first/last-row content hashes, constraints and the target SQL version. Recorded heap, index and total bytes were unchanged in **{sizes_equal}/{len(cases)}** cases. These are counts and sampled content checks, not a full content checksum or application compatibility test.

{chr(10).join(counts)}

The harness verified each published stack deleted, database absent, snapshots absent and managed secret absent before admitting another case. Independent retained-backup verification is separate from an AWS deletion request. Resource absence is not a confirmation that delayed billing reports have settled.

## Cost and reproduction

Using the dated [AWS pricing receipt](../evidence/rds-instance-pricing.json) and the full recorded lifecycle duration gives an approximate compute-plus-allocated-storage estimate of **${total_estimate:.2f}** for the published cases. This is an elapsed-time estimate, not an invoice, and excludes snapshot storage, public IPv4, Secrets Manager, transfer and taxes. The batch's admission reservations total at most $30 and are not an AWS-enforced spending cap.

Follow the [run guide](rds-upgrade-timing.md) to reproduce the selected synthetic cases. PostgreSQL 16.8 was reported deprecated and rejected for the selected new-instance provisioning combination in our dated Ohio API check; these tests use 16.15 and do not measure a 16.8 upgrade. The actual deprecation date is unknown. [Version/orderability receipt](../evidence/rds-version-orderability.json).

Regenerate this report offline with `python3 scripts/report-rds-upgrade-timing.py`. The script reads public receipts and pricing only and makes no AWS calls. Execution commits are preserved separately from report-generation code:

{provenance}

The fixture, infrastructure and probe logic are fixed across these cases; intervening harness changes tightened version/export guards and retained budget reservations. Service-log collection was made repeatable after the initial two manual API captures. Consult each receipt's source hashes rather than assuming the current checkout is the execution version.

## What remains internal

An actual-data snapshot rehearsal and a cleanup-versus-no-cleanup comparison belong in the approved internal environment. This synthetic study does not establish that deleting a large payload table will halve downtime. The minimal read probe does not measure write availability, pooled-connection recovery or application query latency. Cross-region active-active behavior, application recovery, custom parameters, logical replication slots and extension upgrades were not exercised. Keep private identifiers, aggregate counts and real data outside this public repository.
'''
    (ROOT / 'docs/rds-upgrade-results.md').write_text(text)
    print(status)


if __name__ == '__main__':
    main()
