import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location(
    "upgrade_timing",
    Path(__file__).resolve().parents[1] / "scripts/run-upgrade-timing.py",
)
u = importlib.util.module_from_spec(spec)
spec.loader.exec_module(u)


class UpgradeTests(unittest.TestCase):
    def test_old_available_engine_cannot_finish_upgrade(self):
        cluster = {"Status": "available", "EngineVersion": "16.15"}
        writer = {"DBInstanceStatus": "available", "EngineVersion": "16.15"}
        self.assertFalse(u.completed(cluster, writer, "17.11", "16.15"))
        cluster["EngineVersion"] = "17.11"
        self.assertFalse(u.completed(cluster, writer, "17.11", "17.11"))
        writer["EngineVersion"] = "17.11"
        self.assertFalse(u.completed(cluster, writer, "17.11", None))
        self.assertFalse(u.completed(cluster, writer, "17.11", "16.15"))
        self.assertTrue(u.completed(cluster, writer, "17.11", "17.11"))
        writer["DBInstanceStatus"] = "upgrading"
        self.assertFalse(u.completed(cluster, writer, "17.11", "17.11"))

    def test_seed_projects_all_five_relations_in_bounded_nonoverlapping_ranges(self):
        statements = list(u.seed_statements(2050, 1024))
        parents = [
            s
            for s in statements
            if s.startswith("INSERT INTO upgrade_bench.notification_event ")
        ]
        self.assertEqual(len(parents), 3)
        self.assertIn("generate_series(2049,2050)", parents[-1])
        for table in ("delivery_queue", "push_delivery", "send_history"):
            children = [
                s
                for s in statements
                if s.startswith("INSERT INTO upgrade_bench." + table + " ")
            ]
            self.assertEqual(len(children), 3)
            for sql, (lo, hi) in zip(children, [(1, 1024), (1025, 2048), (2049, 2050)]):
                self.assertIn(f"BETWEEN {lo} AND {hi}", sql)
        self.assertFalse(any("PARTITION BY" in s for s in statements))
        self.assertEqual(u.expected_counts(32768)["push_delivery"], 98304)

    def test_probe_gap_is_bounded_and_missing_samples_are_not_zero_downtime(self):
        probes = [
            {"elapsed_seconds": i * 30, "sql_ok": ok}
            for i, ok in enumerate([True, False, False, True])
        ]
        gap = u.availability(probes)
        self.assertEqual(gap["gap_upper_seconds"], 90)
        self.assertEqual(gap["observed_failure_count"], 2)
        self.assertIsNone(
            u.availability([{"elapsed_seconds": 0, "sql_ok": True}])[
                "gap_upper_seconds"
            ]
        )
        self.assertIsNone(u.availability(probes[:-1])["gap_upper_seconds"])


if __name__ == "__main__":
    unittest.main()
