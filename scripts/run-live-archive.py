#!/usr/bin/env python3
"""Synthetic leaf-lock archive with concurrent live inserts and reads on the owned lab."""
import importlib.util
import json
from pathlib import Path
import subprocess
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('retirement', ROOT/'scripts/run-retirement.py')
retirement = importlib.util.module_from_spec(spec)
spec.loader.exec_module(retirement)
lab, archive = retirement.lab, retirement.archive
TABLES, ROWS = retirement.TABLES, retirement.ROWS


def main():
    state = lab.checked_state()
    if state['status'] not in ('CREATE_COMPLETE', 'UPDATE_COMPLETE'):
        raise RuntimeError('Lab is not stable')
    out, region = state['outputs'], state['region']
    capacity = lab.aws(region, 'rds', 'describe-db-clusters', '--db-cluster-identifier', out['ClusterArn'])['DBClusters'][0]['ServerlessV2ScalingConfiguration']
    if capacity['MaxCapacity'] > 1:
        raise RuntimeError('Requires the approved maximum of 1 ACU')
    run = uuid.uuid4().hex
    s = 'live_' + run[:12]
    dest = ROOT/'.lab'/run
    dest.mkdir()
    record = {'passed': False, 'schema': s, 'capacity': capacity, 'exports': {},
              'writes': [], 'reads': [], 'errors': [], 'checks': [], 'blocked_mutations': []}
    base = ['--resource-arn',out['ClusterArn'],'--secret-arn',out['SecretArn'],'--database','retentionlab']
    started = time.monotonic()
    tx = None
    stop = threading.Event()
    threads = []
    stage = 'before_archive'

    def call(action, *args):
        params = base[:4] if action in ('commit-transaction','rollback-transaction') else base
        return lab.aws(region,'rds-data',action,*params,*args)

    def sql(query, transaction=None):
        if time.monotonic()-started > 1800:
            raise RuntimeError('30-minute request guard exhausted')
        return call('execute-statement','--sql',query,*(['--transaction-id',transaction] if transaction else []))

    def values(query, transaction=None):
        return [next(iter(f.values())) for f in sql(query,transaction)['records'][0]]

    def check(name, query, transaction=None):
        if values(query,transaction)[0] is not True:
            raise RuntimeError('Failed: '+name)
        record['checks'].append(name)

    def persist():
        record['elapsed_seconds'] = round(time.monotonic()-started,3)
        (dest/'evidence.json').write_text(json.dumps(record,indent=2)+'\n')

    def writer():
        n = 100000
        try:
            while not stop.is_set():
                n += 1
                phase = stage
                tick = time.monotonic()
                # One auto-committed statement; the ledger is atomic with every relation.
                sql(f'''DO $do$ BEGIN
                  PERFORM set_config('lock_timeout','2s',true);
                  INSERT INTO {s}.event VALUES({n},'2026-10-01 12:00Z',md5('{n}'));
                  INSERT INTO {s}.queue VALUES({n},{n},'2026-10-01 12:00Z','2026-10-01 15:00Z',md5('{n}'));
                  INSERT INTO {s}.push VALUES({n}*2,{n},'2026-10-01 12:00Z',md5('{n}'));
                  INSERT INTO {s}.history VALUES({n},{n},'2026-10-01 12:00Z',md5('{n}'));
                  INSERT INTO {s}.ledger VALUES({n}); END $do$''')
                record['writes'].append({'id':n,'start_phase':phase,'end_phase':stage,
                    'seconds':round(time.monotonic()-tick,3)})
                stop.wait(0.1)
        except Exception as exc:
            record['errors'].append({'worker':'writer','type':type(exc).__name__})
            stop.set()

    def reader():
        try:
            while not stop.is_set():
                phase = stage
                tick = time.monotonic()
                # Root-table scan deliberately overlaps DDL; results must be a coherent statement snapshot.
                result = values(f'''SELECT count(*),NOT EXISTS(
                  SELECT FROM {s}.ledger l LEFT JOIN {s}.event e ON e.id=l.id
                  LEFT JOIN {s}.queue q ON q.event_id=l.id
                  LEFT JOIN {s}.push p ON p.event_id=l.id
                  LEFT JOIN {s}.history h ON h.queue_id=l.id
                  WHERE e.id IS NULL OR q.id IS NULL OR p.id IS NULL OR h.id IS NULL)
                  FROM {s}.event''')
                if result[1] is not True:
                    raise RuntimeError('Reader saw incomplete acknowledged relationships')
                record['reads'].append({'start_phase':phase,'end_phase':stage,'parent_rows':result[0],
                    'seconds':round(time.monotonic()-tick,3)})
                stop.wait(0.1)
        except Exception as exc:
            record['errors'].append({'worker':'reader','type':type(exc).__name__})
            stop.set()

    def wait_progress(phase, minimum=3):
        deadline = time.monotonic()+30
        while time.monotonic()<deadline:
            if record['errors']:
                raise RuntimeError('Worker failed; inspect receipt')
            if all(sum(x['start_phase']==phase and x['end_phase']==phase for x in record[k])>=minimum for k in ('writes','reads')):
                return
            time.sleep(0.1)
        raise RuntimeError('Live traffic did not make required progress: '+phase)

    try:
        record['engine'] = values('SELECT version()')[0]
        sql('CREATE EXTENSION IF NOT EXISTS aws_s3 CASCADE')
        sql(f'CREATE SCHEMA {s}')
        sql(f'CREATE TABLE {s}.event(id bigint,scheduled_at timestamptz,body text NOT NULL,PRIMARY KEY(id,scheduled_at)) PARTITION BY RANGE(scheduled_at)')
        for t in ('queue','push'):
            extra = 'scheduled_at timestamptz NOT NULL,' if t=='queue' else ''
            sql(f'''CREATE TABLE {s}.{t}(id bigint,event_id bigint NOT NULL,parent_scheduled_at timestamptz,
              {extra} body text NOT NULL,PRIMARY KEY(id,parent_scheduled_at),
              FOREIGN KEY(event_id,parent_scheduled_at) REFERENCES {s}.event(id,scheduled_at) ON DELETE CASCADE)
              PARTITION BY RANGE(parent_scheduled_at)''')
            sql(f'CREATE {"UNIQUE " if t=="queue" else ""}INDEX ON {s}.{t}(event_id,parent_scheduled_at)')
        sql(f'CREATE TABLE {s}.history(id bigint,queue_id bigint NOT NULL,parent_scheduled_at timestamptz,body text NOT NULL,PRIMARY KEY(id,parent_scheduled_at)) PARTITION BY RANGE(parent_scheduled_at)')
        sql(f'CREATE TABLE {s}.ledger(id bigint PRIMARY KEY)')
        for t in TABLES:
            for label, date in (('old','2026-09-01'),('live','2026-10-01')):
                sql(f"CREATE TABLE {s}.{t}_{label} PARTITION OF {s}.{t} FOR VALUES FROM ('{date}') TO ('{date}'::date+1)")
            sql(f'CREATE TABLE {s}.{t}_restored(LIKE {s}.{t})')
        for lo in range(1,ROWS+1,256):
            sql(f'''INSERT INTO {s}.event SELECT n,'2026-09-01 12:00Z',string_agg(md5(n::text||':'||b::text),'' ORDER BY b)
              FROM generate_series({lo},{lo+255}) n CROSS JOIN generate_series(1,1024) b GROUP BY n''')
        sql(f'INSERT INTO {s}.queue SELECT id,id,scheduled_at,scheduled_at,left(body,8192) FROM {s}.event')
        sql(f'INSERT INTO {s}.push SELECT e.id*2+b,e.id,scheduled_at,md5(e.id::text||b::text) FROM {s}.event e CROSS JOIN generate_series(0,1) b')
        sql(f'INSERT INTO {s}.history SELECT id,id,parent_scheduled_at,md5(id::text) FROM {s}.queue')
        for worker in (writer,reader):
            thread = threading.Thread(target=worker)
            threads.append(thread)
            thread.start()
        wait_progress('before_archive')
        tx = call('begin-transaction')['transactionId']
        sql("SET LOCAL TIME ZONE 'UTC'",tx)
        sql("SET LOCAL lock_timeout='2s'",tx)
        sql("SET LOCAL statement_timeout='35s'",tx)
        tick = time.monotonic()
        sql('LOCK TABLE '+','.join(f'{s}.{t}_old' for t in TABLES)+' IN SHARE MODE',tx)
        record['leaf_lock_acquire_seconds'] = round(time.monotonic()-tick,3)
        stage = 'archive'
        archive_tick = time.monotonic()
        # Independent sessions attempt every DML family on each expired leaf.
        # Also test parent-routed old inserts and a live-to-old partition move.
        probes = []
        for t in TABLES:
            probes.extend([(t+' update',f'UPDATE {s}.{t}_old SET body=body WHERE id=1'),
                           (t+' delete',f'DELETE FROM {s}.{t}_old WHERE id=1'),
                           (t+' insert',f'INSERT INTO {s}.{t}_old SELECT * FROM {s}.{t}_old LIMIT 1'),
                           (t+' routed insert',f'INSERT INTO {s}.{t} SELECT * FROM {s}.{t}_old LIMIT 1')])
        probes.append(('live-to-old move',f"UPDATE {s}.event SET scheduled_at='2026-09-01 12:00Z' WHERE id=100001"))
        for name, query in probes:
            # Catch only lock timeout. Any successful mutation or other SQLSTATE fails the run.
            sql(f'''DO $do$ BEGIN PERFORM set_config('lock_timeout','250ms',true);
              BEGIN {query}; RAISE EXCEPTION 'Mutation unexpectedly admitted';
              EXCEPTION WHEN lock_not_available THEN NULL; END; END $do$''')
            record['blocked_mutations'].append(name)
        for t in TABLES:
            key = f'runs/{run}/{t}.csv'
            uri = f"aws_commons.create_s3_uri('{out['Bucket']}','{key}','{region}')"
            result = values(f"SELECT * FROM aws_s3.query_export_to_s3('SELECT * FROM {s}.{t}_old ORDER BY id',{uri},options := 'format csv')",tx)
            expected = ROWS*(2 if t=='push' else 1)
            if result[:2] != [expected,1]:
                raise RuntimeError('Unexpected export shape')
            metadata = archive.verified_object_metadata(lab.aws(region,'s3api','head-object','--bucket',out['Bucket'],'--key',key))
            sql(f"SELECT aws_s3.table_import_from_s3('{s}.{t}_restored','','(format csv)',{uri})",tx)
            check(t+' exact restore',f'''SELECT NOT EXISTS(
              (SELECT * FROM {s}.{t}_old EXCEPT ALL SELECT * FROM {s}.{t}_restored) UNION ALL
              (SELECT * FROM {s}.{t}_restored EXCEPT ALL SELECT * FROM {s}.{t}_old))''',tx)
            record['exports'][t] = {'rows':expected,'bytes':result[2],'verified':True,**metadata}
        wait_progress('archive')
        retirement.require_verified(record['exports'])
        record['archive_and_probes_seconds'] = round(time.monotonic()-archive_tick,3)
        persist()
        stage = 'retire'
        tick = time.monotonic()
        # Upgrade only for retirement. Readers and writers can briefly wait here.
        sql('LOCK TABLE '+','.join(f'{s}.{t}' for t in TABLES)+' IN ACCESS EXCLUSIVE MODE',tx)
        sql(f'''DO $do$ BEGIN
          DROP TABLE {s}.history_old; DROP TABLE {s}.push_old; DROP TABLE {s}.queue_old;
          ALTER TABLE {s}.event DETACH PARTITION {s}.event_old; DROP TABLE {s}.event_old;
          END $do$''',tx)
        call('commit-transaction','--transaction-id',tx)
        tx = None
        record['parent_lock_through_commit_seconds'] = round(time.monotonic()-tick,3)
        stage = 'after_retire'
        wait_progress('after_retire')
        stop.set()
        for thread in threads:
            thread.join()
        if record['errors']:
            raise RuntimeError('Concurrent worker failed')
        ids = ','.join(str(x['id']) for x in record['writes'])
        check('client acknowledgements equal committed ledger',f'SELECT count(*)={len(record["writes"])} AND bool_and(id IN ({ids})) FROM {s}.ledger')
        for t in TABLES:
            column = 'scheduled_at' if t=='event' else 'parent_scheduled_at'
            check(t+' live rows and payloads preserved',f'''SELECT count(*)={len(record['writes'])}
              AND bool_and({column}='2026-10-01 12:00Z' AND body=md5(({'id/2' if t=='push' else 'id'})::text))
              FROM {s}.{t}''')
            check(t+' expired partition absent',f"SELECT to_regclass('{s}.{t}_old') IS NULL")
        check('every acknowledged relationship and field matches',f'''SELECT NOT EXISTS(
          SELECT FROM {s}.ledger l
          LEFT JOIN {s}.event e ON e.id=l.id AND e.scheduled_at='2026-10-01 12:00Z'
          LEFT JOIN {s}.queue q ON q.id=l.id AND q.event_id=l.id AND q.parent_scheduled_at=e.scheduled_at
          LEFT JOIN {s}.push p ON p.id=l.id*2 AND p.event_id=l.id AND p.parent_scheduled_at=e.scheduled_at
          LEFT JOIN {s}.history h ON h.id=l.id AND h.queue_id=l.id AND h.parent_scheduled_at=e.scheduled_at
          WHERE e.id IS NULL OR q.id IS NULL OR p.id IS NULL OR h.id IS NULL
          OR q.scheduled_at IS DISTINCT FROM '2026-10-01 15:00Z'::timestamptz)''')
        check('cascade FKs remain validated',f"SELECT count(*)=2 FROM pg_constraint WHERE connamespace='{s}'::regnamespace AND contype='f' AND conparentid=0 AND convalidated AND confrelid='{s}.event'::regclass AND confdeltype='c'")
        record['passed'] = True
    finally:
        stop.set()
        try:
            if tx:
                call('rollback-transaction','--transaction-id',tx)
        finally:
            for thread in threads:
                thread.join()
            persist()
            print('Evidence:',dest/'evidence.json',flush=True)
    print('PASS: live traffic, mutation fence, exact archives, acknowledged rows, retirement',flush=True)


if __name__=='__main__':
    try:
        main()
    except subprocess.CalledProcessError as exc:
        raise SystemExit(exc.stderr)
