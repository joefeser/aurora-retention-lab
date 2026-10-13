import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('rds_log_capture', Path(__file__).resolve().parents[1] / 'scripts/capture-rds-upgrade-logs.py')
u = importlib.util.module_from_spec(spec)
spec.loader.exec_module(u)


class LogCaptureTests(unittest.TestCase):
    def test_only_static_labels_cross_the_public_boundary(self):
        raw = 'private endpoint and identifier\nLinking user relation files   \nUpgrade Complete\n'
        self.assertEqual(['Linking user relation files', 'Upgrade Complete'], u.public_lines(raw))

    def test_label_with_embedded_private_data_is_not_published(self):
        self.assertEqual([], u.public_lines('Upgrade Complete: private account\nLinking user relation files private endpoint'))

    def test_copy_phase_is_kept_if_the_service_really_reports_it(self):
        self.assertEqual(['Copying user relation files'], u.public_lines('Copying user relation files'))
