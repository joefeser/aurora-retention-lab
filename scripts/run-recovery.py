#!/usr/bin/env python3
"""Bounded Aurora query-shape, abandoned archive, and transactional DDL recovery probes."""
import importlib.util
import json
from pathlib import Path
import subprocess
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('archive', ROOT/'scripts/run-aws.py')
archive = importlib.util.module_from_spec(spec)
spec.loader.exec_module(archive)
lab = archive.lab


def main():
    state = lab.checked_state()
    if state['status'] not in ('CREATE_COMPLETE','UPDATE_COMPLETE'):
        raise RuntimeError('Lab is not stable')
    out, region = state['outputs'], state['region']
    cap = lab.aws(region,'rds','describe-db-clusters','--db-cluster-identifier',out['ClusterArn'])['DBClusters'][0]['ServerlessV2ScalingConfiguration']
    if cap['MaxCapacity'] > 1:
        raise RuntimeError('Requires maximum 1 ACU')
    run = uuid.uuid4().hex
    s = 'recover_'+run[:12]
    dest = ROOT/'.lab'/run
    dest.mkdir()
    record = {'passed':False,'capacity':cap,'checks':[],'query_shapes':{},'exports':[]}
    base = ['--resource-arn',out['ClusterArn'],'--secret-arn',out['SecretArn'],'--database','retentionlab']
    transactions = set()
    started = time.monotonic()

    def call(action,*args):
        return lab.aws(region,'rds-data',action,*(base[:4] if action in ('commit-transaction','rollback-transaction') else base),*args)

    def sql(query,tx=None):
        if time.monotonic()-started>900:
            raise RuntimeError('15-minute request guard exhausted')
        return call('execute-statement','--sql',query,*(['--transaction-id',tx] if tx else []))

    def scalar(query,tx=None):
        return next(iter(sql(query,tx)['records'][0][0].values()))

    def begin():
        tx=call('begin-transaction')['transactionId']
        transactions.add(tx)
        sql("SET LOCAL TIME ZONE 'UTC'",tx)
        sql("SET LOCAL lock_timeout='250ms'",tx)
        sql("SET LOCAL statement_timeout='35s'",tx)
        return tx

    def end(tx,commit=False):
        call('commit-transaction' if commit else 'rollback-transaction','--transaction-id',tx)
        transactions.remove(tx)

    def check(name,query,tx=None):
        if scalar(query,tx) is not True:
            raise RuntimeError('Failed: '+name)
        record['checks'].append(name)

    def same(t,restore):
        return f'''SELECT NOT EXISTS((SELECT * FROM {s}.{t}_old EXCEPT ALL SELECT * FROM {s}.{restore})
          UNION ALL (SELECT * FROM {s}.{restore} EXCEPT ALL SELECT * FROM {s}.{t}_old))'''

    def probe(query,tx=None):
        return scalar("SELECT "+s+".probe('"+query.replace("'","''")+"')",tx)

    def export(t,label,tx):
        key=f'runs/{run}/{label}-{t}.csv'
        uri=f"aws_commons.create_s3_uri('{out['Bucket']}','{key}','{region}')"
        result=sql(f"SELECT * FROM aws_s3.query_export_to_s3('SELECT * FROM {s}.{t}_old ORDER BY id',{uri},options := 'format csv')",tx)['records'][0]
        vals=[next(iter(v.values())) for v in result]
        if vals[:2]!=[100,1]:
            raise RuntimeError('Unexpected archive shape')
        metadata=archive.verified_object_metadata(lab.aws(region,'s3api','head-object','--bucket',out['Bucket'],'--key',key))
        record['exports'].append({'table':t,'attempt':label,'rows':100,'bytes':vals[2],'encryption':metadata['encryption']})
        return uri,key

    try:
        record['engine']=scalar('SELECT version()')
        sql('CREATE EXTENSION IF NOT EXISTS aws_s3 CASCADE')
        sql(f'CREATE SCHEMA {s}')
        sql(f'CREATE TABLE {s}.event(id bigint,ts timestamptz,body text,PRIMARY KEY(id,ts)) PARTITION BY RANGE(ts)')
        sql(f'''CREATE TABLE {s}.queue(id bigint,event_id bigint,ts timestamptz,body text,PRIMARY KEY(id,ts),
          FOREIGN KEY(event_id,ts) REFERENCES {s}.event(id,ts) ON DELETE CASCADE) PARTITION BY RANGE(ts)''')
        sql(f'CREATE INDEX ON {s}.queue(event_id,ts)')
        for t in ('event','queue'):
            for label,day in (('old','2026-09-01'),('live','2026-10-01')):
                sql(f"CREATE TABLE {s}.{t}_{label} PARTITION OF {s}.{t} FOR VALUES FROM ('{day}') TO ('{day}'::date+1)")
            for suffix in ('baseline','stale','fresh'):
                sql(f'CREATE TABLE {s}.{t}_{suffix}(LIKE {s}.{t})')
        sql(f"INSERT INTO {s}.event SELECT n,'2026-09-01 12:00Z',md5(n::text) FROM generate_series(1,100)n")
        sql(f"INSERT INTO {s}.event VALUES(101,'2026-10-01 12:00Z','live')")
        sql(f'INSERT INTO {s}.queue SELECT id,id,ts,body FROM {s}.event')
        for t in ('event','queue'):
            sql(f'INSERT INTO {s}.{t}_baseline SELECT * FROM {s}.{t}_old')
        # A deliberate exception rolls back successful mutations within a subtransaction.
        sql(f'''CREATE FUNCTION {s}.probe(q text) RETURNS text LANGUAGE plpgsql AS $fn$
          BEGIN PERFORM set_config('lock_timeout','250ms',true);
          BEGIN EXECUTE q; RAISE SQLSTATE 'Z0001';
          EXCEPTION WHEN SQLSTATE 'Z0001' THEN RETURN 'admitted_rolled_back';
          WHEN lock_not_available THEN RETURN 'blocked_55P03'; END; END $fn$''')
        tx=begin()
        sql(f'LOCK TABLE {s}.event_old,{s}.queue_old IN SHARE MODE',tx)
        shapes={
          'live parent UPDATE by id':f"UPDATE {s}.event SET body='changed' WHERE id=101",
          'live parent UPDATE with partition key':f"UPDATE {s}.event SET body='changed' WHERE id=101 AND ts='2026-10-01 12:00Z'",
          'live queue UPDATE by id':f"UPDATE {s}.queue SET body='changed' WHERE id=101",
          'live queue UPDATE with partition key':f"UPDATE {s}.queue SET body='changed' WHERE id=101 AND ts='2026-10-01 12:00Z'",
          'live parent cascading DELETE by id':f'DELETE FROM {s}.event WHERE id=101',
          'live parent cascading DELETE with partition key':f"DELETE FROM {s}.event WHERE id=101 AND ts='2026-10-01 12:00Z'",
          'expired UPDATE':f"UPDATE {s}.event SET body='changed' WHERE id=1 AND ts='2026-09-01 12:00Z'"}
        for name,query in shapes.items():
            result=probe(query)
            if result not in ('admitted_rolled_back','blocked_55P03'):
                raise RuntimeError('Unexpected probe result')
            record['query_shapes'][name]=result
        if record['query_shapes']['expired UPDATE']!='blocked_55P03':
            raise RuntimeError('Expired write fence failed')
        abandoned={t:export(t,'abandoned',tx) for t in ('event','queue')}
        end(tx)  # Explicitly abandon an exported attempt, simulating a confirmed cancellation.
        for t,(uri,key) in abandoned.items():
            archive.verified_object_metadata(lab.aws(region,'s3api','head-object','--bucket',out['Bucket'],'--key',key))
            check(t+' unchanged after abandoned transaction',same(t,t+'_baseline'))
            sql(f"SELECT aws_s3.table_import_from_s3('{s}.{t}_stale','','(format csv)',{uri})")
        record['checks'].append('S3 objects survive database rollback')
        sql(f"UPDATE {s}.event SET body='late committed change' WHERE id=1 AND ts='2026-09-01 12:00Z'")
        check('old archive is stale after fence release',same('event','event_stale').replace('SELECT NOT EXISTS','SELECT EXISTS',1))
        # A fresh attempt acquires new locks and re-exports; stale objects are never promoted.
        tx=begin()
        sql(f'LOCK TABLE {s}.event_old,{s}.queue_old IN SHARE MODE',tx)
        for t in ('event','queue'):
            uri,_=export(t,'retry',tx)
            sql(f"SELECT aws_s3.table_import_from_s3('{s}.{t}_fresh','','(format csv)',{uri})",tx)
            check(t+' retry archive matches current source',same(t,t+'_fresh'),tx)
        reader=begin()
        scalar(f'SELECT count(*) FROM {s}.event',reader)
        if probe(f'LOCK TABLE {s}.event,{s}.queue IN ACCESS EXCLUSIVE MODE',tx)!='blocked_55P03':
            raise RuntimeError('Long reader failed to block retirement')
        check('reader-blocked retirement leaves source intact',same('event','event_fresh'),tx)
        end(reader)
        # Force failure after the first DROP; the subtransaction must restore the leaf and FK.
        if probe(f'DROP TABLE {s}.queue_old',tx)!='admitted_rolled_back':
            raise RuntimeError('Partial DDL probe failed')
        check('failed partial DDL restores child partition',f"SELECT to_regclass('{s}.queue_old') IS NOT NULL",tx)
        check('failed partial DDL restores child contents',same('queue','queue_fresh'),tx)
        sql(f'LOCK TABLE {s}.event,{s}.queue IN ACCESS EXCLUSIVE MODE',tx)
        sql(f'''DO $do$ BEGIN DROP TABLE {s}.queue_old;
          ALTER TABLE {s}.event DETACH PARTITION {s}.event_old; DROP TABLE {s}.event_old; END $do$''',tx)
        end(tx,commit=True)
        check('fresh request reconciles committed retirement',f"SELECT to_regclass('{s}.event_old') IS NULL AND to_regclass('{s}.queue_old') IS NULL")
        check('live pair unchanged after all probes and retry',f"SELECT (SELECT count(*) FROM {s}.event)=1 AND (SELECT count(*) FROM {s}.queue)=1 AND EXISTS(SELECT FROM {s}.event e JOIN {s}.queue q ON (q.event_id,q.ts)=(e.id,e.ts) WHERE e.id=101 AND e.body='live' AND q.id=101 AND q.body='live')")
        check('cascade FK survives recovery',f"SELECT count(*)=1 FROM pg_constraint WHERE connamespace='{s}'::regnamespace AND contype='f' AND conparentid=0 AND convalidated AND confrelid='{s}.event'::regclass AND confdeltype='c'")
        record['passed']=True
    finally:
        try:
            for tx in list(transactions):
                end(tx)
        finally:
            record['elapsed_seconds']=round(time.monotonic()-started,3)
            (dest/'evidence.json').write_text(json.dumps(record,indent=2)+'\n')
            print('Evidence:',dest/'evidence.json',flush=True)
    print(json.dumps(record,indent=2),flush=True)


if __name__=='__main__':
    try:
        main()
    except subprocess.CalledProcessError as exc:
        raise SystemExit(exc.stderr)
