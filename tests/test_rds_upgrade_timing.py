import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
from tempfile import TemporaryDirectory

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

    def test_out_of_scope_major_upgrade_is_refused_before_aws(self):
        lab = object.__new__(u.Lab)
        lab.rds = Mock()
        with self.assertRaisesRegex(ValueError, 'only PostgreSQL 16 to 17'):
            lab.versions('16.15', '18.3', 'db.r7g.xlarge')
        lab.rds.describe_db_engine_versions.assert_not_called()

    def test_unreviewed_private_field_blocks_publication(self):
        data = {key: None for key in u.PUBLIC_FIELDS}
        data.update(passed=True, stage='complete',
                    cleanup='verified_stack_deleted_database_snapshots_secret_absent')
        self.assertEqual(set(u.PUBLIC_FIELDS), set(u.public_receipt(data)))
        data['private_endpoint'] = 'private.example.invalid'
        with self.assertRaisesRegex(ValueError, 'approved public schema'):
            u.public_receipt(data)

    def test_budget_keeps_original_reservations_when_configuration_changes(self):
        previous = [{'budget_reservation_usd': 8}, {'budget_reservation_usd': 8},
                    {'budget_reservation_usd': 7}]
        self.assertEqual(30, u.reserved_total(previous, 7))
        self.assertEqual(31, u.reserved_total(previous, 8))
        with self.assertRaisesRegex(ValueError, 'Invalid'):
            u.reserved_total([{'budget_reservation_usd': float('nan')}], 7)


    def test_boolean_reservation_is_not_money(self):
        with self.assertRaisesRegex(ValueError, 'Invalid'):
            u.reserved_total([{'budget_reservation_usd': True}], 7)

    def test_missing_stack_does_not_release_inventory_or_delete(self):
        lab = Mock()
        lab.stack.return_value = None
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, 'outcome unresolved'):
                u.cleanup(lab, Path(directory), {'run_id': 'mine'})
        lab.cf.delete_stack.assert_not_called()
        lab.db.assert_not_called()

    def test_unknown_upgrade_state_does_not_delete_stack(self):
        lab = Mock()
        lab.stack.return_value = {'StackId': 'owned', 'StackStatus': 'CREATE_COMPLETE'}
        lab.db.return_value = {'DBInstanceStatus': 'upgrading', 'EngineVersion': '16.15'}
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, 'Upgrade outcome unresolved'):
                u.cleanup(lab, Path(directory), {'run_id': 'mine', 'upgrade_requested': True,
                    'target_version': '17.11'})
        lab.cf.delete_stack.assert_not_called()

    def test_recovery_cannot_take_active_run_lock(self):
        with TemporaryDirectory() as directory, patch.object(u, 'PRIVATE', Path(directory)):
            with u.run_lock():
                with self.assertRaises(BlockingIOError):
                    with u.run_lock():
                        self.fail('Concurrent recovery acquired active run lock')
            with u.run_lock():
                pass

    def test_missing_receipt_requires_reconciliation(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'inventory.json'
            with self.assertRaisesRegex(ValueError, 'Missing prior receipt'):
                u.prior_reservations([path])

    def test_reads_retry_transient_errors_but_not_access_denied(self):
        from botocore.exceptions import EndpointConnectionError, ClientError
        call = Mock(side_effect=[EndpointConnectionError(endpoint_url='https://example.invalid'), {'ok': True}])
        with patch.object(u.time, 'sleep'):
            self.assertEqual({'ok': True}, u.read_with_retry(call))
        self.assertEqual(2, call.call_count)
        call = Mock(side_effect=ClientError({'Error': {'Code': 'AccessDenied'}}, 'DescribeStacks'))
        with self.assertRaises(ClientError):
            u.read_with_retry(call)
        self.assertEqual(1, call.call_count)


    def test_malformed_receipts_require_reconciliation(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'inventory.json'
            receipt = path.parent / 'receipt.json'
            for raw in ['{', 'null', '[]', '"text"', '{}',
                        '{"budget_reservation_usd":true}', '{"budget_reservation_usd":"7"}',
                        '{"budget_reservation_usd":-1}', '{"budget_reservation_usd":NaN}']:
                with self.subTest(raw=raw):
                    receipt.write_text(raw)
                    with self.assertRaisesRegex(ValueError, 'reconcile private inventory'):
                        u.prior_reservations([path])
            receipt.write_text('{"budget_reservation_usd":8}')
            self.assertEqual([{'budget_reservation_usd': 8}], u.prior_reservations([path]))

    def test_confirmed_deletion_then_absence_and_recovery(self):
        lab = Mock()
        lab.stack.side_effect = [{'StackId': 'owned', 'StackStatus': 'CREATE_COMPLETE'}, None]
        lab.db.return_value = None
        lab.rds.get_paginator.return_value.paginate.return_value = []
        state = {'run_id': 'mine'}
        with TemporaryDirectory() as directory:
            u.cleanup(lab, Path(directory), state)
            saved = json.loads((Path(directory) / 'inventory.json').read_text())
            self.assertEqual('owned', saved['stack_delete_accepted'])
            self.assertEqual('owned', saved['stack_deletion_observed'])
            lab.stack.side_effect = None
            lab.stack.return_value = None
            u.cleanup(lab, Path(directory), saved)
        lab.cf.delete_stack.assert_called_once_with(StackName='owned')
        self.assertEqual('verified_stack_deleted_database_snapshots_secret_absent', saved['cleanup'])

    def test_observed_terminal_deletion_is_saved_before_absent_poll(self):
        lab = Mock()
        lab.stack.side_effect = [{'StackId': 'owned', 'StackStatus': 'DELETE_COMPLETE'}, None]
        lab.db.return_value = None
        lab.rds.get_paginator.return_value.paginate.return_value = []
        with TemporaryDirectory() as directory:
            state = {'run_id': 'mine'}
            u.cleanup(lab, Path(directory), state)
            self.assertEqual('owned', state['stack_deletion_observed'])
        lab.cf.delete_stack.assert_not_called()

    def test_unbound_deletion_does_not_authorize_absence(self):
        for state in [{'stack_id': 'owned'}, {'stack_id': 'owned', 'stack_delete_accepted': 'other'},
                      {'stack_deletion_observed': 'owned'}]:
            with self.subTest(state=state):
                self.assertFalse(u.deletion_confirmed(state))


if __name__ == '__main__':
    unittest.main()
