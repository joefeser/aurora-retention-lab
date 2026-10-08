#!/usr/bin/env python3
"""Small synthetic S3 round-trip and coordinated retirement on the recorded lab."""
import importlib.util
import json
from pathlib import Path
import subprocess
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('lab', ROOT / 'scripts' / 'aws-lab.py')
lab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lab)


def main():
    state = lab.checked_state()
    if state['status'] not in ('CREATE_COMPLETE', 'UPDATE_COMPLETE'):
        raise RuntimeError('Wait for CREATE_COMPLETE before running the experiment')
    region = state['region']
    out = state['outputs']
    run_id = uuid.uuid4().hex
    schema = 'poc_' + run_id[:12]
    prefix = 'runs/' + run_id + '/'
    dest = ROOT / '.lab' / run_id
    dest.mkdir()
    record = {'run_id': run_id, 'schema': schema, 'checks': [], 'exports': [], 'passed': False}
    base = ['--resource-arn', out['ClusterArn'], '--secret-arn', out['SecretArn'], '--database', 'retentionlab']
    transaction = None

    def call(command, *args):
        parameters = base[:4] if command in ('commit-transaction', 'rollback-transaction') else base
        return lab.aws(region, 'rds-data', command, *parameters, *args)

    def sql(statement):
        args = ['--sql', statement]
        if transaction:
            args += ['--transaction-id', transaction]
        return call('execute-statement', *args)

    def scalar(statement):
        field = sql(statement)['records'][0][0]
        return next(iter(field.values()))

    def check(label, statement):
        if scalar(statement) is not True:
            raise RuntimeError('Verification failed: ' + label)
        record['checks'].append(label)
        print('PASS:', label, flush=True)

    def same(table):
        return f'''SELECT NOT EXISTS(
          (SELECT * FROM {schema}.{table}_old EXCEPT ALL SELECT * FROM {schema}.{table}_restored)
          UNION ALL
          (SELECT * FROM {schema}.{table}_restored EXCEPT ALL SELECT * FROM {schema}.{table}_old))'''

    started = time.monotonic()
    try:
        record['engine'] = scalar('SELECT version()')
        sql('CREATE EXTENSION IF NOT EXISTS aws_s3 CASCADE')
        sql('CREATE SCHEMA IF NOT EXISTS partman')
        sql('CREATE EXTENSION IF NOT EXISTS pg_partman WITH SCHEMA partman')
        record['extensions'] = sql("SELECT extname, extversion FROM pg_extension WHERE extname IN ('aws_s3','pg_partman')")['records']
        transaction = call('begin-transaction')['transactionId']
        sql("SET LOCAL TIME ZONE 'UTC'")
        sql("SET LOCAL lock_timeout = '2s'")
        sql("SET LOCAL statement_timeout = '35s'")
        sql(f'CREATE SCHEMA {schema}')
        sql(f'''CREATE TABLE {schema}.event (
          id bigint NOT NULL, scheduled_at timestamptz NOT NULL, body text, metadata jsonb,
          PRIMARY KEY(id,scheduled_at)) PARTITION BY RANGE(scheduled_at)''')
        sql(f'''CREATE TABLE {schema}.queue (
          id bigint NOT NULL, event_id bigint NOT NULL, parent_scheduled_at timestamptz NOT NULL,
          scheduled_at timestamptz NOT NULL, body text,
          PRIMARY KEY(id,parent_scheduled_at), UNIQUE(event_id,parent_scheduled_at),
          FOREIGN KEY(event_id,parent_scheduled_at) REFERENCES {schema}.event(id,scheduled_at) ON DELETE CASCADE
          ) PARTITION BY RANGE(parent_scheduled_at)''')
        for table in ('event', 'queue'):
            sql(f"CREATE TABLE {schema}.{table}_old PARTITION OF {schema}.{table} FOR VALUES FROM ('2026-09-01') TO ('2026-09-02')")
            sql(f"CREATE TABLE {schema}.{table}_live PARTITION OF {schema}.{table} FOR VALUES FROM ('2026-10-01') TO ('2026-10-02')")
        sql(f'''INSERT INTO {schema}.event
          SELECT n, '2026-09-01 12:00Z'::timestamptz,
            CASE WHEN n=1 THEN NULL WHEN n=2 THEN '' WHEN n=3 THEN repeat('large synthetic body ',50000)
              ELSE repeat(md5(n::text),512) || E'\\n"quoted", comma, unicode: café' END,
            jsonb_build_object('synthetic',true,'number',n)
          FROM generate_series(1,100) n''')
        sql(f"INSERT INTO {schema}.event VALUES (101,'2026-10-01 12:00Z','live keeper',NULL)")
        sql(f'INSERT INTO {schema}.queue SELECT id,id,scheduled_at,scheduled_at,body FROM {schema}.event')
        sql(f"UPDATE {schema}.queue SET scheduled_at='2026-10-01 15:00Z' WHERE id=1")
        check('mutable delivery time stays in parent-aligned partition',
              f"SELECT tableoid='{schema}.queue_old'::regclass FROM {schema}.queue WHERE id=1")
        # This intentionally blocks writers for the tiny proof. Not a production strategy.
        sql(f'LOCK TABLE {schema}.event, {schema}.queue IN ACCESS EXCLUSIVE MODE')
        sql(f'''DO $test$ BEGIN
          BEGIN
            PERFORM aws_s3.query_export_to_s3('SELECT * FROM {schema}.missing_source',
              aws_commons.create_s3_uri('{out['Bucket']}','{prefix}failed.csv','{region}'), options := 'format csv');
            RAISE EXCEPTION 'Expected export failure did not occur';
          EXCEPTION WHEN undefined_table THEN NULL;
          END;
        END $test$''')
        check('failed export leaves source partition intact',
              f'SELECT count(*)=100 FROM {schema}.event_old')
        for table in ('queue', 'event'):
            sql(f'CREATE TABLE {schema}.{table}_restored (LIKE {schema}.{table} INCLUDING DEFAULTS)')
            key = prefix + table + '.csv'
            export = sql(f"SELECT * FROM aws_s3.query_export_to_s3('SELECT * FROM {schema}.{table}_old ORDER BY id', aws_commons.create_s3_uri('{out['Bucket']}','{key}','{region}'), options := 'format csv')")
            values = [next(iter(f.values())) for f in export['records'][0]]
            if values[0] != 100 or values[1] != 1:
                raise RuntimeError('This bounded fixture requires exactly 100 rows and one export object')
            obj = lab.aws(region, 's3api', 'head-object', '--bucket', out['Bucket'], '--key', key)
            record['exports'].append({'table':table,'rows':values[0],'files':values[1],
                                      'bytes':values[2],'key':key,'etag':obj['ETag']})
            sql(f"SELECT aws_s3.table_import_from_s3('{schema}.{table}_restored','','(format csv)',aws_commons.create_s3_uri('{out['Bucket']}','{key}','{region}'))")
            check(table + ' S3 restore equals source, including duplicates and NULLs', same(table))
        # Prove the verification gate detects corrupt content, then restore it.
        sql(f"UPDATE {schema}.event_restored SET body='corrupted' WHERE id=2")
        check('same-count corrupted restore is rejected', 'SELECT NOT (' + same('event') + ')')
        sql(f"UPDATE {schema}.event_restored SET body='' WHERE id=2")
        check('event verification restored', same('event'))
        # Gate remains inside the transaction, under locks, immediately before retirement.
        check('queue verification before retirement', same('queue'))
        record['column_contract'] = {
            'event': ['id bigint','scheduled_at timestamptz','body text','metadata jsonb'],
            'queue': ['id bigint','event_id bigint','parent_scheduled_at timestamptz','scheduled_at timestamptz','body text'],
        }
        record['copy_options'] = 'format csv'
        manifest = dest / 'verified-export.json'
        manifest.write_text(json.dumps(record, indent=2)+'\n')
        lab.aws(region, 's3api', 'put-object', '--bucket', out['Bucket'],
                '--key', prefix+'verified-export.json', '--body', str(manifest))
        sql(f'DROP TABLE {schema}.queue_old')
        sql(f'ALTER TABLE {schema}.event DETACH PARTITION {schema}.event_old')
        sql(f'DROP TABLE {schema}.event_old')
        check('live parent and child preserved', f'''SELECT
          (SELECT count(*)=1 AND min(id)=101 FROM {schema}.event) AND
          (SELECT count(*)=1 AND min(id)=101 FROM {schema}.queue)''')
        # Maintenance proof is separate; automatic retirement must not bypass verification.
        sql(f'CREATE TABLE {schema}.managed (id bigint, scheduled_at timestamptz NOT NULL) PARTITION BY RANGE(scheduled_at)')
        sql(f"SELECT partman.create_parent(p_parent_table := '{schema}.managed', p_control := 'scheduled_at', p_interval := '1 day', p_premake := 2)")
        sql(f"UPDATE partman.part_config SET retention=NULL WHERE parent_table='{schema}.managed'")
        sql(f"SELECT partman.run_maintenance(p_parent_table := '{schema}.managed')")
        check('pg_partman maintenance creates partitions without background worker',
              f"SELECT count(*)>=3 FROM pg_inherits WHERE inhparent='{schema}.managed'::regclass")
        call('commit-transaction', '--transaction-id', transaction)
        transaction = None
        record['passed'] = True
    finally:
        if transaction:
            try:
                call('rollback-transaction', '--transaction-id', transaction)
            except Exception:
                record['rollback_unconfirmed'] = True
        record['elapsed_seconds'] = round(time.monotonic()-started,2)
        (dest / 'evidence.json').write_text(json.dumps(record,indent=2)+'\n')
        print('Evidence:', dest / 'evidence.json', flush=True)


if __name__ == '__main__':
    try:
        main()
    except subprocess.CalledProcessError as exc:
        raise SystemExit(exc.stderr)
