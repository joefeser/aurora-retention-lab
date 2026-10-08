#!/usr/bin/env python3
"""Two small, quiescent Aurora retirement trials; only the recorded lab is used."""
import importlib.util
import json
from pathlib import Path
import subprocess
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('archive', ROOT/'scripts'/'run-aws.py')
archive = importlib.util.module_from_spec(spec)
spec.loader.exec_module(archive)
lab = archive.lab
TABLES = ('event', 'queue', 'push', 'history')
ROWS = 1024
LIVE = 32


def require_verified(exports):
    if set(exports) != set(TABLES) or any(
        exports[t].get('verified') is not True or exports[t].get('encryption') != 'aws:kms'
        or exports[t].get('rows') != ROWS * (2 if t == 'push' else 1)
        for t in TABLES
    ):
        raise RuntimeError('Retirement requires every related archive to be verified')


def main():
    state = lab.checked_state()
    if state['status'] not in ('CREATE_COMPLETE', 'UPDATE_COMPLETE'):
        raise RuntimeError('Lab is not stable')
    out, region = state['outputs'], state['region']
    capacity = lab.aws(region, 'rds', 'describe-db-clusters', '--db-cluster-identifier',
                       out['ClusterArn'])['DBClusters'][0]['ServerlessV2ScalingConfiguration']
    if capacity['MaxCapacity'] > 1:
        raise RuntimeError('Requires the approved maximum of 1 ACU')
    run = uuid.uuid4().hex
    dest = ROOT/'.lab'/run
    dest.mkdir()
    record = {'passed': False, 'capacity': capacity, 'expired_parents_per_arm': ROWS,
              'live_parents_per_arm': LIVE, 'trials': [],
              'scope': 'Quiescent synthetic comparison; two trials with reversed order; no concurrent writers'}
    base = ['--resource-arn', out['ClusterArn'], '--secret-arn', out['SecretArn'], '--database', 'retentionlab']
    transaction = None
    started = time.monotonic()

    def call(action, *args):
        params = base[:4] if action in ('commit-transaction', 'rollback-transaction') else base
        return lab.aws(region, 'rds-data', action, *params, *args)

    def sql(statement):
        if time.monotonic() - started > 1800:
            raise RuntimeError('30-minute run guard reached')
        args = ['--sql', statement]
        if transaction:
            args += ['--transaction-id', transaction]
        return call('execute-statement', *args)

    def values(statement):
        return [next(iter(f.values())) for f in sql(statement)['records'][0]]

    def check(statement):
        if values(statement)[0] is not True:
            raise RuntimeError('Integrity assertion failed: ' + statement)

    def persist():
        record['elapsed_seconds'] = round(time.monotonic() - started, 3)
        (dest/'evidence.json').write_text(json.dumps(record, indent=2)+'\n')

    try:
        record['engine'] = values('SELECT version()')[0]
        sql('CREATE EXTENSION IF NOT EXISTS aws_s3 CASCADE')
        for trial, order in enumerate((('delete', 'partition'), ('partition', 'delete')), 1):
            for mode in order:
                s = f'retire_{run[:10]}_{trial}_{mode}'
                arm = {'trial': trial, 'mode': mode, 'schema': s, 'exports': {}, 'passed': False}
                record['trials'].append(arm)
                partition = mode == 'partition'
                pk = 'id,scheduled_at' if partition else 'id'
                child_pk = 'id,parent_scheduled_at' if partition else 'id'
                fk = 'event_id,parent_scheduled_at' if partition else 'event_id'
                suffix = ' PARTITION BY RANGE(parent_scheduled_at)' if partition else ''
                sql(f'CREATE SCHEMA {s}')
                sql(f'CREATE TABLE {s}.event(id bigint NOT NULL,scheduled_at timestamptz NOT NULL,body text NOT NULL,PRIMARY KEY({pk}))'
                    + (' PARTITION BY RANGE(scheduled_at)' if partition else ''))
                for t in ('queue', 'push'):
                    sql(f'''CREATE TABLE {s}.{t}(id bigint NOT NULL,event_id bigint NOT NULL,
                      parent_scheduled_at timestamptz NOT NULL,scheduled_at timestamptz NOT NULL,body text NOT NULL,
                      PRIMARY KEY({child_pk}), FOREIGN KEY({fk}) REFERENCES {s}.event({pk}) ON DELETE CASCADE)''' + suffix)
                    sql(f'CREATE {"UNIQUE " if t == "queue" else ""}INDEX ON {s}.{t}({fk})')
                sql(f'''CREATE TABLE {s}.history(id bigint NOT NULL,queue_id bigint NOT NULL,
                  parent_scheduled_at timestamptz NOT NULL,body text NOT NULL,PRIMARY KEY({child_pk}))''' + suffix)
                sql(f'CREATE INDEX ON {s}.history(queue_id)')
                if partition:
                    for t in TABLES:
                        for label, day in (('old', '2026-09-01'), ('live', '2026-10-01')):
                            sql(f"CREATE TABLE {s}.{t}_{label} PARTITION OF {s}.{t} FOR VALUES FROM ('{day}') TO ('{day}'::date+1)")
                for lo in range(1, ROWS+LIVE+1, 256):
                    hi = min(lo+255, ROWS+LIVE)
                    sql(f'''INSERT INTO {s}.event SELECT n,
                      CASE WHEN n<={ROWS} THEN '2026-09-01 12:00Z' ELSE '2026-10-01 12:00Z' END::timestamptz,
                      string_agg(md5(n::text||':'||b::text),'' ORDER BY b)
                      FROM generate_series({lo},{hi}) n CROSS JOIN generate_series(1,1024) b GROUP BY n''')
                sql(f'INSERT INTO {s}.queue SELECT id,id,scheduled_at,scheduled_at,left(body,8192) FROM {s}.event')
                sql(f'INSERT INTO {s}.push SELECT e.id*2+b,e.id,scheduled_at,scheduled_at,md5(e.id::text||b::text) FROM {s}.event e CROSS JOIN generate_series(0,1) b')
                sql(f'INSERT INTO {s}.history SELECT id,id,parent_scheduled_at,md5(id::text) FROM {s}.queue')
                # One expired queue has a live delivery date: retention follows its parent.
                sql(f"UPDATE {s}.queue SET scheduled_at='2026-10-01 15:00Z' WHERE id=1")
                for t in TABLES:
                    sql(f'ANALYZE {s}.{t}')
                transaction = call('begin-transaction')['transactionId']
                sql("SET LOCAL TIME ZONE 'UTC'")
                sql("SET LOCAL lock_timeout='2s'")
                sql("SET LOCAL statement_timeout='35s'")
                lock_start = time.monotonic()
                sql('LOCK TABLE '+','.join(f'{s}.{t}' for t in TABLES)+' IN ACCESS EXCLUSIVE MODE')
                arm['lock_acquire_client_seconds'] = round(time.monotonic()-lock_start, 3)
                archive_start = time.monotonic()
                for t in TABLES:
                    column = 'scheduled_at' if t == 'event' else 'parent_scheduled_at'
                    where = f"{column} < '2026-10-01'::timestamptz"
                    sql(f'CREATE TABLE {s}.{t}_keepers AS SELECT * FROM {s}.{t} WHERE NOT ({where})')
                    sql(f'CREATE TABLE {s}.{t}_restored (LIKE {s}.{t} INCLUDING DEFAULTS)')
                    key = f'runs/{run}/{trial}-{mode}-{t}.csv'
                    query = f'SELECT * FROM {s}.{t} WHERE {where} ORDER BY id'
                    quoted = query.replace("'", "''")
                    uri = f"aws_commons.create_s3_uri('{out['Bucket']}','{key}','{region}')"
                    tick = time.monotonic()
                    exported = values(f"SELECT * FROM aws_s3.query_export_to_s3('{quoted}',{uri},options := 'format csv')")
                    export_seconds = time.monotonic()-tick
                    expected = ROWS * (2 if t == 'push' else 1)
                    if exported[:2] != [expected, 1]:
                        raise RuntimeError('Unexpected archive row/file count')
                    metadata = archive.verified_object_metadata(lab.aws(region, 's3api', 'head-object', '--bucket', out['Bucket'], '--key', key))
                    tick = time.monotonic()
                    sql(f"SELECT aws_s3.table_import_from_s3('{s}.{t}_restored','','(format csv)',{uri})")
                    import_seconds = time.monotonic()-tick
                    check(f'''SELECT NOT EXISTS(
                      (SELECT * FROM {s}.{t} WHERE {where} EXCEPT ALL SELECT * FROM {s}.{t}_restored)
                      UNION ALL (SELECT * FROM {s}.{t}_restored EXCEPT ALL SELECT * FROM {s}.{t} WHERE {where}))''')
                    arm['exports'][t] = {'rows': expected, 'bytes': exported[2], 'verified': True,
                        'export_seconds': round(export_seconds,3), 'import_seconds': round(import_seconds,3), **metadata}
                arm['archive_verify_client_seconds'] = round(time.monotonic()-archive_start,3)
                require_verified(arm['exports'])
                persist()  # Keep verification evidence before destructive SQL.
                sql(f'CREATE TABLE {s}.timing(seconds double precision)')
                if partition:
                    operation = ';'.join(f'DROP TABLE {s}.{t}_old' for t in ('history','push','queue'))
                    operation += f';ALTER TABLE {s}.event DETACH PARTITION {s}.event_old;DROP TABLE {s}.event_old;'
                else:
                    # Soft history is not covered by a foreign key cascade.
                    operation = f'''DELETE FROM {s}.history h USING {s}.queue q, {s}.event e
                      WHERE h.queue_id=q.id AND q.event_id=e.id AND e.scheduled_at<'2026-10-01';
                      DELETE FROM {s}.event WHERE scheduled_at<'2026-10-01';'''
                tick = time.monotonic()
                sql(f'''DO $do$ DECLARE started timestamptz := clock_timestamp(); BEGIN
                  {operation}
                  INSERT INTO {s}.timing VALUES(extract(epoch FROM clock_timestamp()-started)); END $do$''')
                arm['retire_client_seconds'] = round(time.monotonic()-tick,3)
                arm['retire_server_seconds'] = values(f'SELECT seconds FROM {s}.timing')[0]
                for t in TABLES:
                    check(f'''SELECT (SELECT count(*) FROM {s}.{t})={LIVE * (2 if t == 'push' else 1)}
                      AND NOT EXISTS((SELECT * FROM {s}.{t} EXCEPT ALL SELECT * FROM {s}.{t}_keepers)
                      UNION ALL (SELECT * FROM {s}.{t}_keepers EXCEPT ALL SELECT * FROM {s}.{t}))''')
                call('commit-transaction', '--transaction-id', transaction)
                transaction = None
                arm['lock_through_commit_client_seconds'] = round(time.monotonic()-lock_start,3)
                # New transactions prove committed state, not just in-transaction observations.
                for t in TABLES:
                    check(f'SELECT count(*)={LIVE * (2 if t == "push" else 1)} FROM {s}.{t}')
                arm['passed'] = True
                persist()
                print(f'PASS trial {trial} {mode}: retirement {arm["retire_server_seconds"]:.6f}s server; archive/verify {arm["archive_verify_client_seconds"]}s', flush=True)
        record['passed'] = True
    finally:
        try:
            if transaction:
                call('rollback-transaction', '--transaction-id', transaction)
        finally:
            # Preserve failed evidence even when rollback cannot be confirmed.
            persist()
            print('Evidence:', dest/'evidence.json', flush=True)


if __name__ == '__main__':
    try:
        main()
    except subprocess.CalledProcessError as exc:
        raise SystemExit(exc.stderr)
