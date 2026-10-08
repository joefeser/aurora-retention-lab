#!/usr/bin/env python3
"""Run synthetic PostgreSQL evidence in a disposable, network-isolated container."""
import argparse
import json
from pathlib import Path
import subprocess
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', default='postgres:17')
    args = parser.parse_args()
    name = 'aurora-retention-lab-' + uuid.uuid4().hex[:12]
    output = ROOT / '.lab' / name
    output.mkdir(parents=True)
    started = time.monotonic()
    created = False
    try:
        subprocess.run([
            'docker', 'run', '--detach', '--name', name,
            '--label', 'project=aurora-retention-lab', '--network', 'none',
            '--memory', '1g', '--cpus', '2',
            '--tmpfs', '/var/lib/postgresql/data:rw,size=512m',
            '-e', 'POSTGRES_HOST_AUTH_METHOD=trust', args.image,
        ], check=True, capture_output=True, text=True)
        created = True
        for _ in range(60):
            ready = subprocess.run(['docker', 'exec', name, 'pg_isready', '-U', 'postgres'],
                                   capture_output=True)
            if ready.returncode == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError('PostgreSQL did not become ready in 60 seconds')
        image_id = subprocess.check_output(
            ['docker', 'inspect', '--format', '{{.Image}}', name], text=True).strip()
        sql = (ROOT / 'sql' / 'feasibility.sql').read_text()
        result = subprocess.run([
            'docker', 'exec', '-i', name, 'psql', '-X', '-U', 'postgres',
            '-d', 'postgres', '-v', 'ON_ERROR_STOP=1', '-P', 'pager=off',
        ], input=sql, capture_output=True, text=True)
        (output / 'results.txt').write_text(result.stdout + result.stderr)
        metadata = dict(image=args.image, image_id=image_id,
                        elapsed_seconds=round(time.monotonic() - started, 2),
                        passed=result.returncode == 0,
                        scope='local PostgreSQL semantics; not Aurora or production throughput')
        (output / 'run.json').write_text(json.dumps(metadata, indent=2) + '\n')
        print(result.stdout)
        if result.stderr:
            print(result.stderr)
        print(f'Evidence: {output}')
        if result.returncode:
            raise SystemExit(result.returncode)
    finally:
        if created:
            subprocess.run(['docker', 'rm', '--force', '--volumes', name], check=True,
                           capture_output=True)


if __name__ == '__main__':
    main()
