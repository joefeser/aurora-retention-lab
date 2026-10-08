"""Run with the benchmark venv: python benchmarks/test_harness.py."""

import subprocess
import unittest
from unittest.mock import patch, MagicMock
from run_workload import Database


class CleanupTests(unittest.TestCase):
    def test_failed_start_does_not_remove_an_uncreated_container(self):
        with patch.object(
            Database, "_initialize", side_effect=RuntimeError("startup failure")
        ), patch("run_workload.subprocess.run") as command:
            with self.assertRaisesRegex(RuntimeError, "startup failure"):
                Database("postgres:17", "owned-test", {"passed": False})
            command.assert_not_called()

    def test_cleanup_errors_are_recorded_without_masking_receipt(self):
        db = Database.__new__(Database)
        db.target = "postgres:17"
        db.name = "owned-test"
        db.created = True
        db.transaction = True
        db.record = {"passed": True}
        db.connection = MagicMock()
        db.connection.close.side_effect = OSError("connection close")
        with patch.object(db, "rollback", side_effect=OSError("rollback")), patch(
            "run_workload.subprocess.run",
            side_effect=subprocess.TimeoutExpired("docker", 30),
        ):
            db.close()
        self.assertIs(db.record["passed"], False)
        self.assertIs(db.record["container_removed"], False)
        self.assertEqual(db.record["rollback_cleanup_error"], "OSError")
        self.assertEqual(db.record["connection_cleanup_error"], "OSError")
        self.assertEqual(db.record["cleanup_error"], "TimeoutExpired")

    def test_successful_owned_cleanup_has_exact_target(self):
        db = Database.__new__(Database)
        db.name = "owned-test"
        db.created = True
        db.transaction = None
        db.connection = None
        db.record = {"passed": True}
        with patch(
            "run_workload.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0),
        ) as command:
            db.close()
        self.assertEqual(
            command.call_args.args[0], ["docker", "rm", "-f", "owned-test"]
        )
        self.assertIs(db.record["container_removed"], True)
        self.assertIs(db.record["passed"], True)


if __name__ == "__main__":
    unittest.main()
