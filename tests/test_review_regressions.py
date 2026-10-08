import copy
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT/'scripts'/f'{name}.py')
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


prepared = module('run-prepared-plans')
retirement = module('run-retirement')


class PreparedMatrixTests(unittest.TestCase):
    def test_full_matrix_rejects_every_reversed_outcome(self):
        for mode in ('force_custom_plan', 'force_generic_plan'):
            for qualified in (False, True):
                for expired in (False, True):
                    with self.subTest(mode=mode, qualified=qualified, expired=expired):
                        admitted = mode == 'force_custom_plan' and qualified and not expired
                        expected = 'admitted_rolled_back' if admitted else 'blocked_55P03'
                        opposite = 'blocked_55P03' if admitted else 'admitted_rolled_back'
                        prepared.require_fenced_outcome(mode, qualified, expired, expected)
                        with self.assertRaises(RuntimeError):
                            prepared.require_fenced_outcome(mode, qualified, expired, opposite)


class CommitReceiptTests(unittest.TestCase):
    def run_flow(self, failure=None):
        arm = {'passed': False, 'commit_state': 'not_attempted'}
        saved = []
        events = []

        def persist():
            saved.append(copy.deepcopy(arm))
            events.append('persist:'+arm['stage'])

        def commit():
            events.append('commit')
            if failure == 'commit':
                raise RuntimeError('lost commit acknowledgement')

        def verify():
            events.append('verify')
            if failure == 'verify':
                raise RuntimeError('fresh check failed')

        if failure:
            with self.assertRaises(RuntimeError):
                retirement.commit_and_verify(arm, commit, verify, persist)
        else:
            retirement.commit_and_verify(arm, commit, verify, persist)
        return arm, saved, events

    def test_post_commit_failure_preserves_durable_unverified_state(self):
        arm, saved, events = self.run_flow('verify')
        self.assertEqual(arm['commit_state'], 'committed')
        self.assertEqual(arm['stage'], 'committed_unverified')
        self.assertFalse(arm['passed'])
        self.assertEqual(saved[-1], arm)
        self.assertLess(events.index('persist:committed_unverified'), events.index('verify'))

    def test_lost_commit_ack_remains_unknown_without_verification(self):
        arm, saved, events = self.run_flow('commit')
        self.assertEqual(arm['commit_state'], 'unknown')
        self.assertFalse(arm['passed'])
        self.assertEqual(saved[-1], arm)
        self.assertNotIn('verify', events)
        self.assertLess(events.index('persist:commit_pending'), events.index('commit'))

    def test_only_successful_reconciliation_is_complete(self):
        arm, saved, events = self.run_flow()
        self.assertEqual(arm['stage'], 'complete')
        self.assertEqual(arm['commit_state'], 'committed')
        self.assertTrue(arm['passed'])
        self.assertEqual([s['stage'] for s in saved],
                         ['commit_pending', 'committed_unverified', 'complete'])


if __name__ == '__main__':
    unittest.main()
