#!/usr/bin/env python3
"""Bounded 128 MiB / 1 GiB Aurora S3 baseline; never retires source data."""
import argparse
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


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--mib', type=int, choices=(128,1024), default=128)
    args = p.parse_args()
    state = lab.checked_state()
    if state['status'] not in ('CREATE_COMPLETE','UPDATE_COMPLETE'):
        raise RuntimeError('Lab is not stable')
    # Read actual capacity rather than trusting the inventory's intended cap.
    out = state['outputs']
    cluster = lab.aws(state['region'],'rds','describe-db-clusters',
                      '--db-cluster-identifier',out['ClusterArn'])['DBClusters'][0]
    capacity = cluster['ServerlessV2ScalingConfiguration']
    if capacity['MaxCapacity'] > 1:
        raise RuntimeError('Scale run requires the approved 1 ACU maximum')
    run = uuid.uuid4().hex
    schema = 'scale_' + run[:12]
    dest = ROOT/'.lab'/run
    dest.mkdir()
    receipt = {'passed':False,'logical_mib':args.mib,'schema':schema,
               'capacity':capacity,'row_body_bytes':32768,'rows':args.mib*32,
               'batches':[],'scope':'Single-table, bounded chunk export; no concurrent AWS writers or retirement'}
    started = time.monotonic()

    def sql(statement):
        if time.monotonic()-started > 1800:
            raise RuntimeError('30-minute run budget exhausted; retained source and partial archive require inspection')
        return lab.aws(state['region'],'rds-data','execute-statement',
                       '--resource-arn',out['ClusterArn'],'--secret-arn',out['SecretArn'],
                       '--database','retentionlab','--sql',statement)

    def values(statement):
        return [next(iter(f.values())) for f in sql(statement)['records'][0]]

    def persist():
        receipt['elapsed_seconds'] = round(time.monotonic()-started,3)
        (dest/'evidence.json').write_text(json.dumps(receipt,indent=2)+'\n')

    try:
        receipt['engine'] = values('SELECT version()')[0]
        sql(f'CREATE SCHEMA {schema}')
        sql(f'CREATE TABLE {schema}.source(id bigint PRIMARY KEY,body text NOT NULL)')
        sql(f'CREATE TABLE {schema}.restored(LIKE {schema}.source INCLUDING ALL)')
        # Each body has 1024 distinct MD5 blocks. No repeat(single-string) shortcut.
        # This is deterministic hex payload, not a claim about real HTML compressibility.
        load_start = time.monotonic()
        for lo in range(1,receipt['rows']+1,512):
            hi = lo+511
            sql(f'''INSERT INTO {schema}.source
              SELECT n,string_agg(md5(n::text || ':' || b::text),'' ORDER BY b)
              FROM generate_series({lo},{hi}) n CROSS JOIN generate_series(1,1024) b
              GROUP BY n''')
            persist()
        receipt['load_seconds'] = round(time.monotonic()-load_start,3)
        sizes = values(f'SELECT count(*),sum(octet_length(body)),pg_total_relation_size(\'{schema}.source\') FROM {schema}.source')
        if sizes[0] != receipt['rows'] or sizes[1] != args.mib*1024*1024:
            raise RuntimeError('Generated row count or logical byte count differs from requested load')
        receipt['source_relation_bytes'] = sizes[2]
        sql(f'ANALYZE {schema}.source')
        for lo in range(1,receipt['rows']+1,512):
            hi=lo+511
            key=f'runs/{run}/rows-{lo}-{hi}.csv'
            t=time.monotonic()
            exported=values(f"SELECT * FROM aws_s3.query_export_to_s3('SELECT * FROM {schema}.source WHERE id BETWEEN {lo} AND {hi} ORDER BY id',aws_commons.create_s3_uri('{out['Bucket']}','{key}','{state['region']}'),options := 'format csv')")
            export_seconds=time.monotonic()-t
            if exported[0]!=512 or exported[1]!=1:
                raise RuntimeError('Unexpected export shape; no assumptions about missing/multiple objects permitted')
            metadata=archive.verified_object_metadata(lab.aws(state['region'],'s3api','head-object','--bucket',out['Bucket'],'--key',key))
            t=time.monotonic()
            sql(f"SELECT aws_s3.table_import_from_s3('{schema}.restored','','(format csv)',aws_commons.create_s3_uri('{out['Bucket']}','{key}','{state['region']}'))")
            import_seconds=time.monotonic()-t
            # IDs are unique on both sides. Full row equality plus equal counts
            # avoids comparing only hashes or accepting same-count corruption.
            same=values(f'''SELECT
              (SELECT count(*)=512 FROM {schema}.restored WHERE id BETWEEN {lo} AND {hi}) AND
              NOT EXISTS(SELECT FROM {schema}.source s LEFT JOIN {schema}.restored r USING(id)
                         WHERE s.id BETWEEN {lo} AND {hi} AND (r.id IS NULL OR s.body IS DISTINCT FROM r.body))''')[0]
            if same is not True:
                raise RuntimeError('Restored content mismatch')
            receipt['batches'].append({'first_id':lo,'last_id':hi,'rows':exported[0],
                'exported_bytes':exported[2],'export_seconds':round(export_seconds,3),
                'import_seconds':round(import_seconds,3),'verified':True, **metadata})
            persist()
            print(f'Verified {hi}/{receipt["rows"]} rows ({hi//32} MiB)',flush=True)
        final=values(f'SELECT count(*) FROM {schema}.restored')[0]
        if final != receipt['rows']:
            raise RuntimeError('Final restored row count differs')
        receipt['export_seconds_total']=round(sum(b['export_seconds'] for b in receipt['batches']),3)
        receipt['import_seconds_total']=round(sum(b['import_seconds'] for b in receipt['batches']),3)
        receipt['export_mib_per_second_including_api']=round(args.mib/receipt['export_seconds_total'],3)
        receipt['passed']=True
    finally:
        persist()
        print('Evidence:',dest/'evidence.json',flush=True)


if __name__=='__main__':
    try:
        main()
    except subprocess.CalledProcessError as exc:
        raise SystemExit(exc.stderr)
