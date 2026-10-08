import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('ef_sample', ROOT / 'scripts/run-ef-sample.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ReceiptTests(unittest.TestCase):
    def valid(self):
        return json.loads((ROOT / 'evidence/ef-sample.json').read_text())['runs'][0]['application']

    def test_receipt_requires_boolean_and_complete_matrix(self):
        good = self.valid()
        self.assertIs(module.validate_application(good), good)
        for bad in (None, [], {}, {**good, 'passed': 'false'}, {**good, 'passed': 1},
                    {**good, 'engine': None}, {**good, 'probes': good['probes'][:-1]}):
            with self.subTest(value=type(bad).__name__), self.assertRaises(ValueError):
                module.validate_application(bad)

    def test_every_reversed_matrix_result_fails(self):
        good = self.valid()
        for index in range(64):
            changed = copy.deepcopy(good)
            probe = changed['probes'][index]
            probe['outcome'] = 'blocked_55P03' if probe['outcome'] == 'admitted_rolled_back' else 'admitted_rolled_back'
            with self.subTest(index=index), self.assertRaises(ValueError):
                module.validate_application(changed)

    def test_partial_failure_receipt_remains_false(self):
        failed = {**self.valid(), 'passed': False, 'probes': []}
        self.assertIs(module.validate_application(failed)['passed'], False)

    def test_cleanup_failures_preserve_execution_failure_and_write_receipt(self):
        for failure in (subprocess.TimeoutExpired('docker', 30), OSError('docker unavailable'), 1):
            def cleanup(*args, **kwargs):
                if isinstance(failure, Exception):
                    raise failure
                return subprocess.CompletedProcess(args, failure)
            with self.subTest(failure=str(failure)), tempfile.TemporaryDirectory() as directory:
                receipt = {'passed': False, 'execution_failure': {'type': 'ApplicationFailure'}}
                module.finalize_receipt(receipt, Path(directory), 'owned-test', True, time.monotonic(), cleanup)
                actual = json.loads((Path(directory) / 'evidence.json').read_text())
                self.assertFalse(actual['passed'])
                self.assertFalse(actual['container_removed'])
                self.assertEqual(actual['execution_failure'], {'type': 'ApplicationFailure'})
                self.assertIn('cleanup_failure', actual)

    def test_cleanup_failure_revokes_success(self):
        with tempfile.TemporaryDirectory() as directory:
            receipt = {'passed': True}
            module.finalize_receipt(receipt, Path(directory), 'owned-test', True, time.monotonic(),
                                    lambda *a, **kw: subprocess.CompletedProcess(a, 1))
            self.assertIs(receipt['passed'], False)
