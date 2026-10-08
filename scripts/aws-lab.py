#!/usr/bin/env python3
"""Manage only the dedicated synthetic stack recorded in .lab/aws.json."""
import argparse
import datetime
import json
from pathlib import Path
import subprocess
import uuid

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / '.lab' / 'aws.json'


def aws(region, *args):
    result = subprocess.run(['aws', '--region', region, '--output', 'json', *args],
                            check=True, capture_output=True, text=True)
    return json.loads(result.stdout) if result.stdout.strip() else {}


def save(state):
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2) + '\n')


def checked_state():
    state = json.loads(STATE.read_text())
    account = aws(state['region'], 'sts', 'get-caller-identity')['Account']
    if account != state['account']:
        raise RuntimeError('Authenticated account differs from the recorded lab account')
    stack = aws(state['region'], 'cloudformation', 'describe-stacks',
                '--stack-name', state.get('stack_id', state['stack_name']))['Stacks'][0]
    if stack['StackName'] != state['stack_name'] or not any(
        t['Key'] == 'Project' and t['Value'] == 'aurora-retention-lab' for t in stack.get('Tags', [])):
        raise RuntimeError('Stack ownership check failed')
    if state.get('stack_id') and state['stack_id'] != stack['StackId']:
        raise RuntimeError('Stack identity changed')
    state['stack_id'] = stack['StackId']
    state['status'] = stack['StackStatus']
    state['outputs'] = {x['OutputKey']: x['OutputValue'] for x in stack.get('Outputs', [])}
    save(state)
    return state


def main():
    p = argparse.ArgumentParser(description=__doc__)
    s = p.add_subparsers(dest='action', required=True)
    create = s.add_parser('create')
    create.add_argument('--account', required=True)
    create.add_argument('--region', default='us-east-2')
    create.add_argument('--engine-version', default='17.9')
    s.add_parser('status')
    s.add_parser('update')
    s.add_parser('delete')
    args = p.parse_args()
    if args.action == 'create':
        if STATE.exists():
            previous = checked_state()
            if previous['status'] != 'DELETE_COMPLETE':
                raise RuntimeError('An inventory already exists; delete that lab before creating another')
            STATE.rename(STATE.with_name(previous['stack_name'] + '.json'))
        identity = aws(args.region, 'sts', 'get-caller-identity')
        if identity['Account'] != args.account:
            raise RuntimeError('Authenticated account does not match --account')
        template = 'file://' + str(ROOT / 'infra' / 'lab.json')
        aws(args.region, 'cloudformation', 'validate-template', '--template-body', template)
        name = 'aurora-retention-lab-' + uuid.uuid4().hex[:8]
        state = dict(account=args.account, region=args.region, stack_name=name,
                     created_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                     engine_version=args.engine_version, max_acu=1, budget_usd=50)
        # Preserve the exact intended name even if the create call has an unknown outcome.
        save(state)
        response = aws(args.region, 'cloudformation', 'create-stack', '--stack-name', name,
                       '--template-body', template, '--capabilities', 'CAPABILITY_IAM',
                       '--parameters', json.dumps([{'ParameterKey':'EngineVersion','ParameterValue':args.engine_version}]),
                       '--tags', 'Key=Project,Value=aurora-retention-lab',
                       '--timeout-in-minutes', '45')
        state['stack_id'] = response['StackId']
        save(state)
        print('Creation started. Inventory: .lab/aws.json. Run status; no wait is hidden here.')
    elif args.action == 'status':
        state = checked_state()
        print(json.dumps({'stack_name':state['stack_name'], 'status':state['status']}, indent=2))
    elif args.action == 'update':
        state = checked_state()
        if state['status'] not in ('CREATE_COMPLETE', 'UPDATE_COMPLETE'):
            raise RuntimeError('Stack must be stable before update')
        aws(state['region'], 'cloudformation', 'update-stack', '--stack-name', state['stack_id'],
            '--template-body', 'file://' + str(ROOT / 'infra' / 'lab.json'),
            '--capabilities', 'CAPABILITY_IAM', '--parameters',
            json.dumps([{'ParameterKey':'EngineVersion','UsePreviousValue':True}]))
        print('Update started. Run status until UPDATE_COMPLETE.')
    else:
        state = checked_state()
        if state['status'] == 'DELETE_COMPLETE':
            print('Stack deletion already complete.')
            return
        # Recover bucket identity even if create rolled back before producing outputs.
        resources = aws(state['region'], 'cloudformation', 'list-stack-resources',
                        '--stack-name', state['stack_id'])['StackResourceSummaries']
        buckets = [r['PhysicalResourceId'] for r in resources
                   if r['ResourceType']=='AWS::S3::Bucket' and r['ResourceStatus']!='DELETE_COMPLETE']
        for bucket in buckets:
            # The lab deliberately does not enable versioning or Object Lock.
            versioning = aws(state['region'], 's3api', 'get-bucket-versioning', '--bucket', bucket)
            if versioning.get('Status'):
                raise RuntimeError('Unexpected bucket versioning; inspect versions before deletion')
            aws(state['region'], 's3', 'rm', 's3://' + bucket, '--recursive', '--only-show-errors')
        aws(state['region'], 'cloudformation', 'delete-stack', '--stack-name', state['stack_id'])
        print('Deletion requested. Run status until DELETE_COMPLETE; billing has not yet been confirmed stopped.')


if __name__ == '__main__':
    try:
        main()
    except subprocess.CalledProcessError as exc:
        raise SystemExit(exc.stderr)
