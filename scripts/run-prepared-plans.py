#!/usr/bin/env python3
"""Compare forced custom/generic prepared DML under a leaf fence in isolated local PostgreSQL."""
import argparse
import json
from pathlib import Path
import subprocess
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--image',choices=('postgres:16','postgres:17'),default='postgres:17')
    args=p.parse_args()
    name='retention-prepared-'+uuid.uuid4().hex[:12]
    dest=ROOT/'.lab'/name
    dest.mkdir(parents=True)
    record={'passed':False,'image':args.image,'cases':[],'controls':[],'checks':[]}
    created=False
    holder=None
    started=time.monotonic()
    command=['docker','exec','-i',name,'psql','-XqAt','-U','postgres','-v','ON_ERROR_STOP=1']

    def sql(statement):
        r=subprocess.run(command,input=statement,text=True,capture_output=True,timeout=15)
        if r.returncode:
            raise RuntimeError(r.stderr)
        return r.stdout.strip()

    def check(label,statement):
        if sql(statement)!='t':
            raise RuntimeError(label)
        record['checks'].append(label)

    cases=[]
    for table,operation in (('event','UPDATE'),('queue','UPDATE'),('event','DELETE')):
        for qualified in (False,True):
            query=(f"UPDATE {table} SET body='changed'" if operation=='UPDATE' else f'DELETE FROM {table}')
            query+=' WHERE id=$1'+(' AND ts=$2' if qualified else '')
            cases.append((f'{table} {operation} '+('id+timestamp' if qualified else 'id'),query,qualified,101))
    cases.append(('expired UPDATE id+timestamp',"UPDATE event SET body='changed' WHERE id=$1 AND ts=$2",True,1))

    def run_case(mode,case):
        label,query,qualified,ident=case
        params='bigint,timestamptz' if qualified else 'bigint'
        execute=f'EXECUTE candidate({ident}'+(",''2026-09-01 12:00Z''" if ident==1 else ",''2026-10-01 12:00Z''" if qualified else '')+')'
        lines=sql(f'''SET plan_cache_mode={mode};
          SET statement_timeout='3s';
          PREPARE candidate({params}) AS {query};
          SELECT probe('{execute}');
          SELECT json_build_object('generic_plans',generic_plans,'custom_plans',custom_plans,'configured_mode',current_setting('plan_cache_mode'))
            FROM pg_prepared_statements WHERE name='candidate';''').splitlines()
        if len(lines)!=2 or lines[0] not in ('admitted_rolled_back','blocked_55P03'):
            raise RuntimeError('Unexpected probe output: '+str(lines))
        counters=json.loads(lines[1])
        # A lock timeout can occur before a cached-plan counter increments.
        key='generic_plans' if mode=='force_generic_plan' else 'custom_plans'
        if counters['configured_mode']!=mode or counters[key] not in ((1,) if lines[0]=='admitted_rolled_back' else (0,1)) or counters['custom_plans' if key=='generic_plans' else 'generic_plans']!=0:
            raise RuntimeError('Prepared execution did not use the selected plan mode')
        return {'shape':label,'plan_mode':mode,'outcome':lines[0],**counters}

    try:
        subprocess.run(['docker','run','-d','--pull','never','--name',name,'--network','none',
          '--label','project=aurora-retention-lab','--memory','1g','--cpus','2',
          '--tmpfs','/var/lib/postgresql/data:rw,size=512m','-e','POSTGRES_HOST_AUTH_METHOD=trust',args.image],check=True,capture_output=True)
        created=True
        for _ in range(60):
            if subprocess.run(['docker','exec',name,'pg_isready','-U','postgres'],capture_output=True).returncode==0:
                break
            time.sleep(0.5)
        else:
            raise RuntimeError('Startup deadline reached')
        record['engine']=sql('SELECT version()')
        record['image_id']=subprocess.run(['docker','inspect','--format','{{.Image}}',name],capture_output=True,text=True,check=True).stdout.strip()
        sql('''CREATE TABLE event(id bigint,ts timestamptz,body text,PRIMARY KEY(id,ts)) PARTITION BY RANGE(ts);
          CREATE TABLE queue(id bigint,event_id bigint,ts timestamptz,body text,PRIMARY KEY(id,ts),
            FOREIGN KEY(event_id,ts) REFERENCES event(id,ts) ON DELETE CASCADE) PARTITION BY RANGE(ts);
          CREATE INDEX ON queue(event_id,ts);
          CREATE TABLE event_old PARTITION OF event FOR VALUES FROM ('2026-09-01') TO ('2026-09-02');
          CREATE TABLE event_live PARTITION OF event FOR VALUES FROM ('2026-10-01') TO ('2026-10-02');
          CREATE TABLE queue_old PARTITION OF queue FOR VALUES FROM ('2026-09-01') TO ('2026-09-02');
          CREATE TABLE queue_live PARTITION OF queue FOR VALUES FROM ('2026-10-01') TO ('2026-10-02');
          INSERT INTO event VALUES(1,'2026-09-01 12:00Z','old'),(101,'2026-10-01 12:00Z','live');
          INSERT INTO queue SELECT id,id,ts,body FROM event;
          CREATE TABLE event_baseline AS TABLE event;
          CREATE TABLE queue_baseline AS TABLE queue;
          CREATE FUNCTION probe(q text) RETURNS text LANGUAGE plpgsql AS $fn$
          BEGIN PERFORM set_config('lock_timeout','250ms',true);
          BEGIN EXECUTE q; RAISE SQLSTATE 'Z0001';
          EXCEPTION WHEN SQLSTATE 'Z0001' THEN RETURN 'admitted_rolled_back';
          WHEN lock_not_available THEN RETURN 'blocked_55P03'; END; END $fn$;''')
        for mode in ('force_custom_plan','force_generic_plan'):
            for case in cases:
                result=run_case(mode,case)
                record['controls'].append(result)
                if result['outcome']!='admitted_rolled_back':
                    raise RuntimeError('Unfenced control did not execute')
        record['checks'].append('all 14 unfenced controls admitted with verified plan counters')
        holder=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        holder.stdin.write("SET statement_timeout='3s'; BEGIN; LOCK TABLE event_old,queue_old IN SHARE MODE; SELECT 'fence_ready';\n")
        holder.stdin.flush()
        if holder.stdout.readline().strip()!='fence_ready':
            raise RuntimeError('Fence was not acquired')
        for mode in ('force_custom_plan','force_generic_plan'):
            for case in cases:
                result=run_case(mode,case)
                if (case[0].startswith('expired') or not case[2]) and result['outcome']!='blocked_55P03':
                    raise RuntimeError('Expected expired/unqualified fence probe was admitted')
                record['cases'].append(result)
        holder.stdin.write('ROLLBACK;\n')
        holder.stdin.flush()
        holder.communicate(timeout=5)
        if holder.returncode:
            raise RuntimeError('Fence session failed')
        holder=None
        for t in ('event','queue'):
            check(t+' contents unchanged',f'''SELECT NOT EXISTS((TABLE {t} EXCEPT ALL TABLE {t}_baseline)
              UNION ALL (TABLE {t}_baseline EXCEPT ALL TABLE {t}));''')
        record['passed']=True
    finally:
        if holder:
            holder.terminate()
            try:
                holder.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                holder.kill()
                holder.communicate()
        if created:
            result=subprocess.run(['docker','rm','-f',name],capture_output=True,text=True)
            record['container_removed']=result.returncode==0
            if result.returncode:
                record['passed']=False
        record['elapsed_seconds']=round(time.monotonic()-started,3)
        (dest/'evidence.json').write_text(json.dumps(record,indent=2)+'\n')
        print('Evidence:',dest/'evidence.json',flush=True)
    if not record['passed']:
        raise RuntimeError('Fixture or cleanup failed')
    print(json.dumps(record,indent=2))


if __name__=='__main__':
    main()
