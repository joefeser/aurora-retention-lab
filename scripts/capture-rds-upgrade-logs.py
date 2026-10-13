#!/usr/bin/env python3
"""Read owned RDS service logs; publish static phase labels, never raw messages."""
import argparse
import fcntl
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('rds_runner', ROOT / 'scripts/run-rds-upgrade-timing.py')
u = importlib.util.module_from_spec(spec)
spec.loader.exec_module(u)
PHASE_LABELS = {'Performing Upgrade', 'Linking user relation files', 'Copying user relation files', 'Upgrade Complete'}


def public_lines(raw):
    return [line.strip() for line in raw.splitlines() if line.strip() in PHASE_LABELS]


def require_complete_logs(observations):
    if not any('Upgrade Complete' in x['excerpts'] for x in observations):
        raise ValueError('Incomplete log capture remains private; existing public observations preserved')


def _capture(run_id):
    if not re.fullmatch(r'rds-upgrade-[a-f0-9]{12}', run_id):
        raise ValueError('Invalid run identifier')
    directory = u.PRIVATE / run_id
    state = json.loads((directory / 'inventory.json').read_text())
    receipt = json.loads((directory / 'receipt.json').read_text())
    if state['run_id'] != run_id:
        raise ValueError('Inventory identity mismatch')
    provenance = u.source_receipt()
    dirty = subprocess.check_output(['git', 'status', '--porcelain', '--', 'scripts/capture-rds-upgrade-logs.py'], cwd=ROOT, text=True)
    if dirty.strip():
        raise ValueError('Commit the collector before publishing log observations')
    lab = u.Lab(state['region'], state['account'])
    db = lab.db(state)  # Exact account, stack membership, engine/class and tags.
    logs = [log for page in lab.rds.get_paginator('describe_db_log_files').paginate(
        DBInstanceIdentifier=run_id, FilenameContains='upgrade') for log in page['DescribeDBLogFiles']]
    if len(logs) > 20:
        raise ValueError('Unexpected number of upgrade logs; inspect privately')
    u.save(directory / 'upgrade-log-inventory.json', logs)
    observations = []
    for i, log in enumerate(logs):
        part = lab.rds.download_db_log_file_portion(DBInstanceIdentifier=run_id,
            LogFileName=log['LogFileName'], NumberOfLines=10000)
        raw = part['LogFileData']
        path = directory / f'upgrade-log-{i}.txt'
        path.write_text(raw)
        observations.append({'kind': 'pg_upgrade_internal' if 'internal' in log['LogFileName'] else 'other_upgrade_log',
            'raw_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'captured_bytes': len(path.read_bytes()), 'listed_file_size_bytes': log.get('Size'),
            'additional_data_pending': part.get('AdditionalDataPending', False), 'excerpts': public_lines(raw)})
    record = {'schema_version': 1, 'source': 'RDS DownloadDBLogFilePortion', 'captured_at': u.utc(),
        'tier': receipt['tier'], 'instance_class': receipt['instance_class'],
        'execution_git_head': receipt['provenance']['git_head'],
        'collector_provenance': {'git_head': provenance['git_head'],
            'sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},
        'engine': db['Engine'], 'source_version': receipt['source_version'], 'target_version': receipt['target_version'],
        'observed_control_status': db['DBInstanceStatus'], 'logs': observations,
        'limitations': ['Point-in-time bounded service log capture; raw logs and identifiers remain private.',
            'Phase labels do not measure the complete managed operation or SQL interruption.',
            'The initial two cases used the equivalent manual API capture before this collector existed.']}
    require_complete_logs(observations)
    record['observed_engine_version'] = db['EngineVersion']
    output = ROOT / 'evidence/rds-upgrade-log-observations.json'
    existing = json.loads(output.read_text())['observations'] if output.exists() else []
    # Historical initial captures were both on the documented 32 GiB baseline.
    existing = [x for x in existing if (x['tier'], x.get('instance_class', 'db.r7g.xlarge')) !=
                (record['tier'], record['instance_class'])]
    u.save(output, {'schema_version': 1, 'observations': [*existing, record]})
    print(json.dumps({'tier': record['tier'], 'instance_class': record['instance_class'],
                      'logs_captured': len(logs), 'phase_labels': [x['excerpts'] for x in observations]}))


def capture(run_id):
    u.PRIVATE.mkdir(parents=True, exist_ok=True)
    with (u.PRIVATE / '.log-capture.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _capture(run_id)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id', required=True)
    capture(parser.parse_args().run_id)
