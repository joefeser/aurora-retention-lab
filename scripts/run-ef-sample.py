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
            receipt['application'] = json.loads((destination / 'application.json').read_text())
        if result.returncode:
            raise RuntimeError(f'Application failed; see {destination / "run.log"}')
        receipt['passed'] = receipt['application']['passed']
    finally:
        if created:
            removed = subprocess.run(['docker', 'rm', '-f', name], capture_output=True, timeout=30)
            receipt['container_removed'] = removed.returncode == 0
            receipt['passed'] = receipt['passed'] and receipt['container_removed']
        receipt['elapsed_seconds'] = round(time.monotonic() - started, 3)
        (destination / 'evidence.json').write_text(json.dumps(receipt, indent=2) + '\n')
        print(f'Evidence: {destination / "evidence.json"}', flush=True)
    if not receipt['passed']:
        raise RuntimeError('Application validation or cleanup failed')
    print(f'Passed on {args.image}; temporary container removed')


if __name__ == '__main__':
    main()
