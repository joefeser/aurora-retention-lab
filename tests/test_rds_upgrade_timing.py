import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('rds_upgrade', ROOT / 'scripts/run-rds-upgrade-timing.py')
u = importlib.util.module_from_spec(spec)
spec.loader.exec_module(u)


class RdsUpgradeTests(unittest.TestCase):
    def test_disjoint_interruptions_stay_separate(self):
        samples = [dict(start_seconds=i * 5, end_seconds=i * 5 + 1, sql_ok=okay)
                   for i, okay in enumerate([True, False, True, True, False, True])]
        result = u.interruption(samples)
        self.assertEqual(2, result['failed_samples'])
        self.assertEqual([11, 11], [x['upper_seconds'] for x in result['episodes']])

    def test_incomplete_outages_are_unbounded(self):
        result = u.interruption([dict(start_seconds=0, end_seconds=1, sql_ok=False),
                                 dict(start_seconds=5, end_seconds=6, sql_ok=True),
                                 dict(start_seconds=10, end_seconds=11, sql_ok=False)])
        self.assertEqual([None, None], [x['upper_seconds'] for x in result['episodes']])

    def test_absent_failures_do_not_claim_zero(self):
        result = u.interruption([dict(start_seconds=0, end_seconds=1, sql_ok=True)])
        self.assertEqual([], result['episodes'])
        self.assertIn('does not prove zero', result['note'])

    def test_count_validation_rejects_missing_children(self):
        counts = u.fixture.expected_counts(32768)
        before = {'tables': {n: {'rows': rows} for n, rows in counts.items()}}
        after = json.loads(json.dumps(before))
        self.assertTrue(u.matched(before, after, 32768))
        after['tables']['push_delivery']['rows'] -= 1
        self.assertFalse(u.matched(before, after, 32768))

    def test_standard_rds_not_aurora_and_no_wide_ingress(self):
        template = json.loads((ROOT / 'infra/rds-upgrade.json').read_text())
        db = template['Resources']['Database']['Properties']
        self.assertEqual('postgres', db['Engine'])
        self.assertFalse(db['MultiAZ'])
        self.assertEqual('400', db['AllocatedStorage'])
        ingress = template['Resources']['DatabaseSecurityGroup']['Properties']['SecurityGroupIngress']
        self.assertEqual([{'IpProtocol': 'tcp', 'FromPort': 5432, 'ToPort': 5432,
                           'CidrIp': {'Ref': 'ClientCidr'}}], ingress)
        self.assertTrue(template['Parameters']['ClientCidr']['AllowedPattern'].endswith('/32'))

    def test_wrong_stack_owner_refused(self):
        lab = object.__new__(u.Lab)
        lab.cf = Mock()
        lab.cf.describe_stacks.return_value = {'Stacks': [{'StackId': 'stack', 'Tags': [
            {'Key': 'Project', 'Value': 'aurora-retention-lab'}, {'Key': 'RdsBenchmarkRun', 'Value': 'other'}]}]}
        with self.assertRaisesRegex(ValueError, 'ownership'):
            lab.stack({'run_id': 'mine', 'stack_id': 'stack'})

    def test_wrong_engine_prevents_database_mutation(self):
        lab = object.__new__(u.Lab)
        lab.rds = Mock()
        lab.rds.describe_db_instances.return_value = {'DBInstances': [{'DBInstanceArn': 'arn',
            'Engine': 'aurora-postgresql', 'DBInstanceClass': 'db.r7g.xlarge'}]}
        lab.rds.list_tags_for_resource.return_value = {'TagList': [{'Key': 'RdsBenchmarkRun', 'Value': 'mine'}]}
        with self.assertRaisesRegex(ValueError, 'engine/class'):
            lab.db({'run_id': 'mine', 'instance_class': 'db.r7g.xlarge'})


if __name__ == '__main__':
    unittest.main()
