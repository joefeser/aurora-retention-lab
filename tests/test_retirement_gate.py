import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('retirement', Path(__file__).resolve().parents[1]/'scripts'/'run-retirement.py')
retirement = importlib.util.module_from_spec(spec)
spec.loader.exec_module(retirement)


class RetirementGateTests(unittest.TestCase):
    def fixture(self):
        return {t: {'verified': True, 'encryption': 'aws:kms',
                    'rows': retirement.ROWS*(2 if t == 'push' else 1)}
                for t in retirement.TABLES}

    def test_complete_verified_set(self):
        retirement.require_verified(self.fixture())

    def test_each_missing_relation_blocks_retirement(self):
        for table in retirement.TABLES:
            with self.subTest(table=table):
                exports = self.fixture()
                del exports[table]
                with self.assertRaises(RuntimeError):
                    retirement.require_verified(exports)

    def test_invalid_evidence_blocks_retirement(self):
        for field, value in (('verified', False), ('verified', 'true'),
                             ('encryption', 'AES256'), ('rows', 0)):
            with self.subTest(field=field, value=value):
                exports = self.fixture()
                exports['history'][field] = value
                with self.assertRaises(RuntimeError):
                    retirement.require_verified(exports)


if __name__ == '__main__':
    unittest.main()
