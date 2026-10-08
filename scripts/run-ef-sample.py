#!/usr/bin/env python3
"""Run the EF application fixture in a disposable, loopback-only PostgreSQL container."""
import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / 'samples/Retention.Sample'


def run(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def validate_application(value):
    if not isinstance(value, dict) or type(value.get('passed')) is not bool:
        raise ValueError('Application receipt must be an object with a boolean passed field')
    for field in ('engine', 'framework', 'efVersion', 'npgsqlVersion', 'providerVersion'):
        if not isinstance(value.get(field), str) or not value[field]:
            raise ValueError(f'Missing application receipt field: {field}')
    for field in ('checks', 'sql'):
        if not isinstance(value.get(field), list) or not all(isinstance(x, str) for x in value[field]):
            raise ValueError(f'Invalid application receipt field: {field}')
    if not isinstance(value.get('probes'), list) or not all(isinstance(x, dict) for x in value['probes']):
        raise ValueError('Invalid application probes')
    if value['passed']:
        candidates = {'parent_id_update', 'parent_qualified_update', 'tracked_parent_update', 'queue_reschedule',
                      'parent_id_delete', 'parent_qualified_delete', 'template_delete', 'expired_qualified_update'}
        modes = {'unprepared', 'force_custom_plan', 'force_generic_plan', 'auto'}
        expected = {(c, m, w) for c in candidates for m in modes for w in ('cold', 'warm')}
        actual = {(p.get('candidate'), p.get('mode'), p.get('warmth')) for p in value['probes']}
        if len(value['probes']) != 64 or actual != expected or not value['checks'] or not value['sql']:
            raise ValueError('Successful receipt lacks complete matrix/evidence')
        qualified = {'parent_qualified_update', 'tracked_parent_update', 'queue_reschedule', 'parent_qualified_delete'}
        for p in value['probes']:
            outcome = 'admitted_rolled_back' if p['candidate'] in qualified and p['mode'] != 'force_generic_plan' else 'blocked_55P03'
            if p.get('outcome') != outcome:
                raise ValueError('Successful receipt contradicts the expected matrix')
    return value


def finalize_receipt(receipt, destination, name, created, started, cleanup=subprocess.run):
    # Cleanup failure must not overwrite the execution failure or prevent receipt persistence.
    if created:
        try:
            removed = cleanup(['docker', 'rm', '-f', name], capture_output=True, timeout=30)
            receipt['container_removed'] = removed.returncode == 0
            if removed.returncode:
                receipt['cleanup_failure'] = {'type': 'NonzeroExit', 'returncode': removed.returncode}
        except Exception as exc:
            receipt['container_removed'] = False
            receipt['cleanup_failure'] = {'type': type(exc).__name__}
        receipt['passed'] = receipt['passed'] is True and receipt['container_removed']
    receipt['elapsed_seconds'] = round(time.monotonic() - started, 3)
    (destination / 'evidence.json').write_text(json.dumps(receipt, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', choices=['postgres:16', 'postgres:17'], default='postgres:17')
    args = parser.parse_args()
    name = 'retention-ef-' + uuid.uuid4().hex[:12]
    destination = ROOT / '.lab' / name
    destination.mkdir(parents=True)
    password = secrets.token_hex(24)
    env = os.environ.copy()
    env['POSTGRES_PASSWORD'] = password
    receipt = {'passed': False, 'image': args.image}
    created = False
    started = time.monotonic()
    try:
        run('dotnet', 'restore', str(PROJECT), '--locked-mode', capture_output=True, timeout=120)
        run('dotnet', 'build', str(PROJECT), '--no-restore', '--nologo', capture_output=True, timeout=120)
        created = True  # This invocation owns this random name even if Docker times out.
        run('docker', 'run', '-d', '--pull', 'never', '--name', name,
            '--label', 'project=aurora-retention-lab', '--memory', '1g', '--cpus', '2',
            '--tmpfs', '/var/lib/postgresql/data:rw,size=512m',
            '-p', '127.0.0.1::5432', '-e', 'POSTGRES_PASSWORD', '-e', 'POSTGRES_DB=retention_sample',
            args.image, env=env, capture_output=True, timeout=30)
        for _ in range(60):
            if subprocess.run(['docker', 'exec', name, 'pg_isready', '-h', '127.0.0.1', '-U', 'postgres', '-d', 'retention_sample'], capture_output=True).returncode == 0:
                break
            time.sleep(.5)
        else:
            raise RuntimeError('Database startup deadline reached')
        mapping = run('docker', 'port', name, '5432/tcp', capture_output=True, timeout=10).stdout.strip()
        if not mapping.startswith('127.0.0.1:'):
            raise RuntimeError('Unexpected public port binding')
        port = int(mapping.split(':')[1])
        receipt['image_id'] = run('docker', 'inspect', '--format', '{{.Image}}', name, capture_output=True).stdout.strip()
        env['RETENTION_SAMPLE_CONNECTION'] = f'Host=127.0.0.1;Port={port};Database=retention_sample;Username=postgres;Password={password};Timeout=5;Command Timeout=10'
        result = subprocess.run(['dotnet', 'run', '--project', str(PROJECT), '--no-build', '--', str(destination / 'application.json')],
                                env=env, text=True, capture_output=True, timeout=300)
        # SQL and values are synthetic; never persist the connection string or generated password.
        (destination / 'run.log').write_text((result.stdout + result.stderr).replace(password, '[redacted]'))
        if (destination / 'application.json').exists():
            receipt['application'] = validate_application(json.loads((destination / 'application.json').read_text()))
        if 'application' not in receipt:
            raise ValueError('Missing application receipt')
        if result.returncode:
            raise RuntimeError(f'Application failed; see {destination / "run.log"}')
        receipt['passed'] = receipt['application']['passed']
    except Exception as exc:
        receipt['passed'] = False
        receipt['execution_failure'] = {'type': type(exc).__name__, 'message': str(exc).replace(password, '[redacted]')}
        if isinstance(exc, subprocess.CalledProcessError):
            (destination / 'run.log').write_text(((exc.stdout or '') + (exc.stderr or '')).replace(password, '[redacted]'))
    finally:
        finalize_receipt(receipt, destination, name, created, started)
        print(f'Evidence: {destination / "evidence.json"}', flush=True)
    if not receipt['passed']:
        raise RuntimeError('Application validation or cleanup failed')
    print(f'Passed on {args.image}; temporary container removed')


if __name__ == '__main__':
    main()
