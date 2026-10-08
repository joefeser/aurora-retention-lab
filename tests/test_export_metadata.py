"""Encryption drift must stop export acceptance, not merely alter its receipt."""
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location(
    'runner', Path(__file__).resolve().parents[1] / 'scripts' / 'run-aws.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class ExportEncryptionTests(unittest.TestCase):
    def test_kms_is_recorded(self):
        self.assertEqual(runner.verified_object_metadata(
            {'ETag': 'opaque', 'ServerSideEncryption': 'aws:kms'}),
            {'etag': 'opaque', 'encryption': 'aws:kms'})

    def test_missing_or_wrong_encryption_rejected(self):
        for value in (None, 'AES256', ''):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                obj = {'ETag': 'opaque'}
                if value is not None:
                    obj['ServerSideEncryption'] = value
                runner.verified_object_metadata(obj)


if __name__ == '__main__':
    unittest.main()
