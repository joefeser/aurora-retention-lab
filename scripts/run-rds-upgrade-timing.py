#!/usr/bin/env python3
"""Standard RDS synthetic upgrade timing. Private inventory; explicit public export."""
import argparse
import datetime as dt
import hashlib
import importlib.util
import ipaddress
import json
import math
import os
from pathlib import Path
import subprocess
import threading
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = ROOT / '.lab/rds-upgrade-timing'
TIERS = {'small': 32768, 'medium': 524288, 'large': 2097152}
RESERVE_USD = 8.0  # Conservative admission reservation; never a billing guarantee.
spec = importlib.util.spec_from_file_location('aurora_fixture', ROOT / 'scripts/run-upgrade-timing.py')
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
save = fixture.save
utc = fixture.utc


def interruption(samples):
    """Bound each sampled interruption; retain unbounded leading/trailing outages."""
    episodes = []
    first = None
    previous = None
    for sample in samples:
        if not sample['sql_ok'] and first is None:
            first = sample
        if sample['sql_ok']:
            if first is not None:
                episodes.append({'first_failure_seconds': first['start_seconds'],
                    'last_success_before_seconds': previous['start_seconds'] if previous else None,
                    'first_success_after_seconds': sample['end_seconds'],
                    'upper_seconds': sample['end_seconds'] - previous['start_seconds'] if previous else None})
                first = None
            previous = sample
    if first is not None:
        episodes.append({'first_failure_seconds': first['start_seconds'],
            'last_success_before_seconds': previous['start_seconds'] if previous else None,
            'first_success_after_seconds': None, 'upper_seconds': None})
    return {'episodes': episodes, 'failed_samples': sum(not p['sql_ok'] for p in samples),
        'note': 'Fresh TLS connection + SELECT 1 probes; envelopes include connection/query time. No failed samples does not prove zero downtime. Multiple episodes remain separate.'}


def matched(before, after, parents):
    expected = fixture.expected_counts(parents)
    return all(before['tables'][name]['rows'] == after['tables'][name]['rows'] == count
               for name, count in expected.items())


def source_receipt():
    paths = ['scripts/run-rds-upgrade-timing.py', 'scripts/run-upgrade-timing.py',
             'infra/rds-upgrade.json', 'benchmarks/workload.py']
    dirty = subprocess.check_output(['git', 'status', '--porcelain', '--', *paths], cwd=ROOT, text=True)
    if dirty.strip():
        raise ValueError('Commit benchmark sources before live execution')
    return {'git_head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
            'sha256': {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in paths}}


class Lab:
    def __init__(self, region, account):
        import boto3
        from botocore.config import Config
        # Mutations are single-attempt. Read failures stop rather than rerun mutations.
        cfg = Config(connect_timeout=5, read_timeout=60, retries={'total_max_attempts': 1})
        self.rds = boto3.client('rds', region_name=region, config=cfg)
        self.cf = boto3.client('cloudformation', region_name=region, config=cfg)
        self.secrets = boto3.client('secretsmanager', region_name=region, config=cfg)
        if boto3.client('sts', region_name=region, config=cfg).get_caller_identity()['Account'] != account:
            raise ValueError('Authenticated account differs from local authorized inventory')
        self.account = account
        self.region = region

    def versions(self, source, target, instance):
        v = self.rds.describe_db_engine_versions(Engine='postgres', EngineVersion=source)['DBEngineVersions']
        if len(v) != 1 or v[0]['Status'] != 'available' or not any(
            t['EngineVersion'] == target and t['IsMajorVersionUpgrade'] for t in v[0]['ValidUpgradeTarget']):
            raise ValueError('Source is unavailable or direct major upgrade target is unsupported')
        for version in (source, target):
            options = self.rds.describe_orderable_db_instance_options(Engine='postgres', EngineVersion=version,
                                                                    DBInstanceClass=instance)['OrderableDBInstanceOptions']
            if not any(o.get('StorageType') == 'gp3' for o in options):
                raise ValueError('Requested version/class/gp3 is not orderable')

    def stack(self, state):
        try:
            stack = self.cf.describe_stacks(StackName=state.get('stack_id', state['run_id']))['Stacks'][0]
        except self.cf.exceptions.ClientError as exc:
            if exc.response['Error']['Code'] == 'ValidationError' and 'does not exist' in exc.response['Error']['Message']:
                return None
            raise
        tags = {t['Key']: t['Value'] for t in stack.get('Tags', [])}
        if tags.get('RdsBenchmarkRun') != state['run_id'] or tags.get('Project') != 'aurora-retention-lab':
            raise ValueError('Stack ownership mismatch')
        if state.get('stack_id') and stack['StackId'] != state['stack_id']:
            raise ValueError('Stack identity mismatch')
        return stack

    def db(self, state, absent_ok=False):
        try:
            db = self.rds.describe_db_instances(DBInstanceIdentifier=state['run_id'])['DBInstances'][0]
        except self.rds.exceptions.DBInstanceNotFoundFault:
            if absent_ok:
                return None
            raise
        tags = {t['Key']: t['Value'] for t in self.rds.list_tags_for_resource(ResourceName=db['DBInstanceArn'])['TagList']}
        if tags.get('RdsBenchmarkRun') != state['run_id']:
            raise ValueError('Database ownership mismatch')
        if db['Engine'] != 'postgres' or db['DBInstanceClass'] != state['instance_class']:
            raise ValueError('Database engine/class differs from owned case')
        stack = self.stack(state)
        if stack is None:
            raise ValueError('Database exists without owned stack; reconcile before mutation')
        resources = self.cf.describe_stack_resources(StackName=stack['StackId'])['StackResources']
        if not any(r['LogicalResourceId'] == 'Database' and r.get('PhysicalResourceId') == state['run_id'] for r in resources):
            raise ValueError('Database does not belong to stack')
        return db

    def credentials(self, db):
        secret = db['MasterUserSecret']['SecretArn']
        result = json.loads(self.secrets.get_secret_value(SecretId=secret)['SecretString'])
        return {'host': db['Endpoint']['Address'], 'port': db['Endpoint']['Port'], 'dbname': 'benchmark',
                'user': result['username'], 'password': result['password'], 'sslmode': 'verify-full',
                'sslrootcert': str(PRIVATE / 'global-bundle.pem'), 'connect_timeout': 5,
                'options': '-c statement_timeout=120000 -c lock_timeout=10000'}


def measure(conn):
    tables = {}
    for name in fixture.w.TABLES:
        relation = f'{fixture.SCHEMA}.{name}'
        rows = conn.execute(f'SELECT count(*) FROM {relation}').fetchone()[0]
        sizes = conn.execute('SELECT pg_total_relation_size(%s::regclass), pg_relation_size(%s::regclass), pg_indexes_size(%s::regclass)', (relation, relation, relation)).fetchone()
        tables[name] = dict(zip(('rows', 'total_bytes', 'heap_bytes', 'index_bytes'), (rows, *sizes)))
        # Bounded content checks supplement counts; these are explicitly sampled.
        tables[name]['sampled_row_hashes'] = conn.execute(
            f'SELECT id, md5(to_jsonb(t)::text) FROM {relation} t '
            f'WHERE id IN (SELECT min(id) FROM {relation}) '
            f'OR id IN (SELECT max(id) FROM {relation}) ORDER BY id'
        ).fetchall()
    version, total = conn.execute("SELECT current_setting('server_version'), pg_database_size(current_database())").fetchone()
    constraints = conn.execute(
        'SELECT conname, contype, convalidated FROM pg_constraint '
        'WHERE connamespace=%s::regnamespace ORDER BY conname', (fixture.SCHEMA,)
    ).fetchall()
    return {'server_version': version, 'database_bytes': total, 'tables': tables,
            'constraints': constraints}


def wait_for(check, seconds, label):
    end = time.monotonic() + seconds
    while True:
        if check():
            return
        if time.monotonic() > end:
            raise TimeoutError(label)
        time.sleep(20)


def cleanup(lab, directory, state):
    stack = lab.stack(state)
    db = lab.db(state, absent_ok=True)
    if db:
        state['secret_arn'] = db.get('MasterUserSecret', {}).get('SecretArn')
    # Inventory exact-instance snapshots before deleting their parent; identities stay private.
    snapshots = [s for page in lab.rds.get_paginator('describe_db_snapshots').paginate(DBInstanceIdentifier=state['run_id'])
                 for s in page['DBSnapshots']]
    state.setdefault('snapshots', [])
    for snap in snapshots:
        if snap['DBInstanceIdentifier'] != state['run_id']:
            raise ValueError('Snapshot identity mismatch')
        if snap['SnapshotType'] == 'manual' and snap['DBSnapshotIdentifier'] not in state['snapshots']:
            state['snapshots'].append(snap['DBSnapshotIdentifier'])
    save(directory / 'inventory.json', state)
    if stack and stack['StackStatus'] not in ('DELETE_IN_PROGRESS', 'DELETE_COMPLETE'):
        lab.cf.delete_stack(StackName=stack['StackId'])
    def deleted():
        current = lab.stack(state)
        if current and current['StackStatus'] == 'DELETE_FAILED':
            raise RuntimeError('Cleanup failed; inspect private stack events')
        return current is None or current['StackStatus'] == 'DELETE_COMPLETE'
    wait_for(deleted, 3600, 'Stack deletion timeout')
    for identifier in state['snapshots']:
        try:
            snap = lab.rds.describe_db_snapshots(DBSnapshotIdentifier=identifier)['DBSnapshots'][0]
        except lab.rds.exceptions.DBSnapshotNotFoundFault:
            continue
        if snap['DBInstanceIdentifier'] != state['run_id']:
            raise ValueError('Snapshot identity mismatch')
        if snap['Status'] != 'deleting':
            lab.rds.delete_db_snapshot(DBSnapshotIdentifier=identifier)
    def snapshots_absent():
        return not any(page['DBSnapshots'] for page in lab.rds.get_paginator('describe_db_snapshots').paginate(DBInstanceIdentifier=state['run_id']))
    wait_for(snapshots_absent, 1800, 'Snapshot deletion timeout')
    if lab.db(state, absent_ok=True) is not None:
        raise ValueError('Database remains after cleanup')
    secret = state.get('secret_arn')
    if secret:
        try:
            metadata = lab.secrets.describe_secret(SecretId=secret)
        except lab.secrets.exceptions.ResourceNotFoundException:
            metadata = None
        if metadata is not None:
            raise ValueError('Managed secret remains; reconcile its lifecycle')
    state['cleanup'] = 'verified_stack_deleted_database_snapshots_secret_absent'
    state['cleanup_at'] = utc()
    save(directory / 'inventory.json', state)


def run(args):
    import fcntl
    import psycopg
    PRIVATE.mkdir(parents=True, exist_ok=True)
    if not math.isfinite(args.budget_usd) or not 0 < args.budget_usd <= 30:
        raise ValueError('This authorized batch has a maximum $30 planning budget')
    with (PRIVATE / '.run.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        source = source_receipt()
        if not (PRIVATE / 'global-bundle.pem').is_file():
            raise ValueError('Download the documented AWS TLS certificate bundle first')
        prior = [json.loads(p.read_text()) for p in PRIVATE.glob('*/inventory.json')]
        if any(s.get('cleanup') != 'verified_stack_deleted_database_snapshots_secret_absent' for s in prior):
            raise ValueError('Previous run needs reconciliation/cleanup before another case')
        if (len(prior) + 1) * RESERVE_USD > args.budget_usd:
            raise ValueError('Admission reservations exceed batch planning budget')
        local = json.loads((ROOT / '.lab/aws.json').read_text())
        lab = Lab(args.region, local['account'])
        lab.versions(args.source, args.target, args.instance_class)
        with urllib.request.urlopen('https://checkip.amazonaws.com', timeout=10) as response:
            client_ip = ipaddress.IPv4Address(response.read().decode().strip())
        run_id = 'rds-upgrade-' + uuid.uuid4().hex[:12]
        directory = PRIVATE / run_id
        state = {'run_id': run_id, 'region': args.region, 'account': lab.account,
                 'instance_class': args.instance_class, 'created_at': utc(), 'tier': args.tier,
                 'source_version': args.source, 'target_version': args.target, 'client_cidr': f'{client_ip}/32'}
        receipt = {'schema_version': 1, 'engine': 'postgres', 'tier': args.tier, 'source_version': args.source,
                   'target_version': args.target, 'instance_class': args.instance_class, 'region': args.region,
                   'allocated_storage_gib': 400, 'storage_type': 'gp3', 'multi_az': False,
                   'parents': TIERS[args.tier], 'provenance': source, 'created_at': utc(), 'stage': 'creating',
                   'sql_samples': [], 'control_samples': [], 'passed': False,
                   'budget_reservation_usd': RESERVE_USD}
        save(directory / 'inventory.json', state)
        save(directory / 'receipt.json', receipt)
        stop = threading.Event()
        thread = None
        started = time.monotonic()
        print(f'{args.tier}: creation requested; private inventory saved', flush=True)
        try:
            # State is durable before a potentially unknown mutation outcome.
            result = lab.cf.create_stack(StackName=run_id, TemplateBody=(ROOT / 'infra/rds-upgrade.json').read_text(),
                Parameters=[{'ParameterKey': k, 'ParameterValue': v} for k, v in
                    {'RunId': run_id, 'SourceVersion': args.source, 'InstanceClass': args.instance_class, 'ClientCidr': state['client_cidr']}.items()],
                Tags=[{'Key': 'RdsBenchmarkRun', 'Value': run_id}, {'Key': 'Project', 'Value': 'aurora-retention-lab'}],
                ClientRequestToken=run_id, TimeoutInMinutes=60, OnFailure='DELETE')
            state['stack_id'] = result['StackId']
            save(directory / 'inventory.json', state)
            def ready():
                stack = lab.stack(state)
                if stack is None or stack['StackStatus'] in ('CREATE_FAILED', 'ROLLBACK_COMPLETE', 'DELETE_COMPLETE'):
                    raise RuntimeError('Creation failed; see private CloudFormation events')
                return stack['StackStatus'] == 'CREATE_COMPLETE'
            wait_for(ready, 3900, 'Creation deadline')
            db = lab.db(state)
            if db['EngineVersion'] != args.source or db['DBInstanceStatus'] != 'available':
                raise ValueError('Created database does not match source version/readiness')
            if db['AllocatedStorage'] != 400 or db['StorageType'] != 'gp3' or db['MultiAZ']:
                raise ValueError('Created database storage/topology does not match the case')
            receipt['observed_storage'] = {k: db.get(k) for k in
                ('AllocatedStorage', 'StorageType', 'Iops', 'StorageThroughput', 'StorageEncrypted')}
            credentials = lab.credentials(db)
            state['secret_arn'] = db['MasterUserSecret']['SecretArn']
            save(directory / 'inventory.json', state)
            receipt['provision_seconds'] = time.monotonic() - started
            receipt['stage'] = 'loading'
            save(directory / 'receipt.json', receipt)
            print(f'{args.tier}: source ready; loading synthetic fixture', flush=True)
            load_start = time.monotonic()
            with psycopg.connect(**credentials, autocommit=True) as conn:
                for statement in fixture.seed_statements(TIERS[args.tier], chunk=8192):
                    conn.execute(statement)
                    if time.monotonic() - load_start > 3600:
                        raise TimeoutError('Fixture load exceeded one hour; stop before upgrade')
                receipt['load_seconds'] = time.monotonic() - load_start
                receipt['before'] = measure(conn)
            if not matched(receipt['before'], receipt['before'], TIERS[args.tier]):
                raise ValueError('Synthetic graph count mismatch before upgrade')
            receipt['stage'] = 'upgrading'
            upgrade_start = time.monotonic()
            def probe_once():
                p = {'start_seconds': time.monotonic() - upgrade_start, 'sql_ok': False}
                try:
                    probe_creds = dict(credentials, options='-c statement_timeout=5000')
                    with psycopg.connect(**probe_creds, autocommit=True) as conn:
                        one, version = conn.execute("SELECT 1, current_setting('server_version')").fetchone()
                        p.update(sql_ok=one == 1, server_version=version)
                except Exception as exc:
                    # Exception text may contain endpoint/username; never put it in public receipts.
                    p['error_type'] = type(exc).__name__
                p['end_seconds'] = time.monotonic() - upgrade_start
                receipt['sql_samples'].append(p)
            probe_once()  # Baseline must be successful before the mutation.
            if not receipt['sql_samples'][-1]['sql_ok']:
                raise ValueError('No successful pre-upgrade SQL probe')
            def probing():
                while not stop.wait(5):
                    probe_once()
            thread = threading.Thread(target=probing, daemon=True)
            thread.start()
            receipt['request_started_seconds'] = time.monotonic() - upgrade_start
            receipt['request_at'] = utc()
            save(directory / 'receipt.json', receipt)
            print(f'{args.tier}: direct probes active; requesting major upgrade once', flush=True)
            lab.rds.modify_db_instance(DBInstanceIdentifier=run_id, EngineVersion=args.target,
                                       AllowMajorVersionUpgrade=True, ApplyImmediately=True)
            receipt['request_returned_seconds'] = time.monotonic() - upgrade_start
            while True:
                db = lab.db(state)
                elapsed = time.monotonic() - upgrade_start
                receipt['control_samples'].append({'seconds': elapsed, 'status': db['DBInstanceStatus'],
                                                   'engine_version': db['EngineVersion']})
                sql = receipt['sql_samples'][-1]
                save(directory / 'receipt.json', receipt)
                if db['DBInstanceStatus'] == 'available' and db['EngineVersion'] == args.target and sql['sql_ok'] and sql.get('server_version', '').split(' ')[0] == args.target:
                    receipt['request_to_ready_seconds'] = elapsed - receipt['request_started_seconds']
                    break
                if elapsed > args.upgrade_timeout_minutes * 60:
                    raise TimeoutError('Upgrade observation deadline; no automatic retry')
                time.sleep(20)
            stop.set()
            thread.join(timeout=20)
            if thread.is_alive():
                raise RuntimeError('SQL probe thread did not stop')
            receipt['interruption'] = interruption(receipt['sql_samples'])
            receipt['stage'] = 'validating'
            validate_start = time.monotonic()
            with psycopg.connect(**credentials, autocommit=True) as conn:
                receipt['after'] = measure(conn)
                for table in fixture.w.TABLES:
                    conn.execute(f'ANALYZE {fixture.SCHEMA}.{table}')
            receipt['validation_seconds'] = time.monotonic() - validate_start
            receipt['checks'] = {
                'expected_counts_preserved': matched(receipt['before'], receipt['after'], TIERS[args.tier]),
                'sampled_rows_preserved': all(receipt['before']['tables'][n]['sampled_row_hashes'] ==
                    receipt['after']['tables'][n]['sampled_row_hashes'] for n in fixture.w.TABLES),
                'constraints_preserved': receipt['before']['constraints'] == receipt['after']['constraints'],
                'target_sql_version': receipt['after']['server_version'].split(' ')[0] == args.target,
            }
            receipt['passed'] = all(receipt['checks'].values())
            events = lab.rds.describe_events(SourceIdentifier=run_id, SourceType='db-instance',
                StartTime=dt.datetime.fromisoformat(receipt['request_at']))['Events']
            save(directory / 'aws-events.json', events)
            receipt['event_categories'] = [{'date': str(e['Date']), 'categories': e['EventCategories']} for e in events]
            if not receipt['passed']:
                raise ValueError('Post-upgrade synthetic graph count mismatch')
            print(f"{args.tier}: upgrade validated in {receipt['request_to_ready_seconds']:.1f}s; cleaning owned resources", flush=True)
        except BaseException as exc:
            receipt['failure_type'] = type(exc).__name__
            # Raw exception is private. Do not infer rollback from an ambiguous exception.
            (directory / 'error.txt').write_text(str(exc))
            raise
        finally:
            stop.set()
            if thread:
                thread.join(timeout=20)
            receipt['stage'] = 'cleaning'
            save(directory / 'receipt.json', receipt)
            try:
                cleanup(lab, directory, state)
                receipt['cleanup'] = state['cleanup']
                receipt['stage'] = 'complete' if receipt['passed'] else 'incomplete'
            finally:
                receipt['total_seconds'] = time.monotonic() - started
                save(directory / 'receipt.json', receipt)
            print(f'{args.tier}: cleanup verified', flush=True)


def publish():
    receipts = []
    for path in sorted(PRIVATE.glob('*/receipt.json')):
        data = json.loads(path.read_text())
        if not data.get('passed') or data.get('cleanup') != 'verified_stack_deleted_database_snapshots_secret_absent' or data.get('stage') != 'complete':
            raise ValueError('Only validated and cleaned cases may be published')
        receipts.append(data)
    if not receipts:
        raise ValueError('No RDS receipts to publish')
    save(ROOT / 'evidence/upgrade-timing-rds.json', {'schema_version': 1, 'engine': 'postgres', 'cases': receipts,
         'limitations': ['Synthetic single-AZ fixtures, not real-data stage/prod rehearsal.',
            'Each case is one trial. No production downtime guarantee or linear size extrapolation.',
            'Control-plane states are not isolated pg_upgrade phases. SQL interruption uses sampled fresh connections.',
            'Budget reservations and estimated prices are not actual billed costs.']})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    run_parser = sub.add_parser('run')
    run_parser.add_argument('--tier', choices=TIERS, required=True)
    run_parser.add_argument('--source', default='16.15')
    run_parser.add_argument('--target', default='17.11')
    run_parser.add_argument('--region', choices=['us-east-2'], default='us-east-2')
    run_parser.add_argument('--instance-class', choices=['db.r7g.xlarge', 'db.r7g.large'], default='db.r7g.xlarge')
    run_parser.add_argument('--budget-usd', type=float, default=30)
    run_parser.add_argument('--upgrade-timeout-minutes', type=int, choices=range(10, 121), default=60)
    sub.add_parser('publish')
    clean = sub.add_parser('cleanup')
    clean.add_argument('--run-id', required=True)
    args = parser.parse_args()
    if args.command == 'run':
        run(args)
    elif args.command == 'publish':
        publish()
    else:
        if not __import__('re').fullmatch(r'rds-upgrade-[a-f0-9]{12}', args.run_id):
            raise ValueError('Invalid run identifier')
        directory = PRIVATE / args.run_id
        state = json.loads((directory / 'inventory.json').read_text())
        if state['run_id'] != args.run_id:
            raise ValueError('Inventory identity mismatch')
        cleanup(Lab(state['region'], state['account']), directory, state)


if __name__ == '__main__':
    main()
