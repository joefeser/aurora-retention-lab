import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('rds_report', ROOT / 'scripts/report-rds-upgrade-timing.py')
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)


class ReportTests(unittest.TestCase):
    def test_historical_configuration_supported_and_drift_rejected(self):
        cases = json.loads((ROOT / 'evidence/upgrade-timing-rds.json').read_text())['cases']
        r.validate_configuration(cases)
        changed = json.loads(json.dumps(cases))
        changed[0]['target_version'] = '17.12'
        with self.assertRaises(ValueError):
            r.validate_configuration(changed)
        changed = json.loads(json.dumps(cases))
        changed[0]['observed_storage']['Iops'] = 3000
        with self.assertRaises(ValueError):
            r.validate_configuration(changed)

    def test_unobserved_and_unbounded_are_distinct(self):
        self.assertEqual('no observed interruption', r.sql_label({'sql_episode_count': 0, 'sql_episode_upper_seconds': None}))
        self.assertEqual('unbounded', r.sql_label({'sql_episode_count': 1, 'sql_episode_upper_seconds': None}))
