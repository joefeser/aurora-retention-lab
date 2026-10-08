"""Run with the benchmark venv: python benchmarks/test_harness.py."""

import subprocess
import json
from pathlib import Path
import tempfile
import observe_cloud
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


class CloudCollectorTests(unittest.TestCase):
    def test_malformed_metric_and_api_error_do_not_discard_other_metrics(self):
        good = {
            "Datapoints": [
                {
                    "Timestamp": "2026-10-08T15:00:00+00:00",
                    "Unit": "Count",
                    "Minimum": 1,
                    "Average": 2,
                    "Maximum": 3,
                }
            ]
        }
        client = MagicMock()
        client.get_metric_statistics.side_effect = [
            {"Datapoints": [None, *good["Datapoints"]]},
            RuntimeError("private service detail"),
            good,
            good,
            good,
            good,
            good,
        ]
        db = MagicMock()
        db.outputs = {"ClusterArn": "synthetic"}
        db.region = "synthetic"
        db.rds.describe_db_clusters.return_value = {
            "DBClusters": [
                {
                    "DBClusterIdentifier": "synthetic",
                    "DBClusterMembers": [{"DBInstanceIdentifier": "synthetic"}],
                }
            ]
        }
        db.scalar.return_value = "synthetic engine"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".lab").mkdir()
            with patch.object(observe_cloud, "ROOT", root), patch.object(
                observe_cloud, "Database", return_value=db
            ), patch.object(observe_cloud.boto3, "client", return_value=client):
                observe_cloud.main()
            record = json.loads(
                next((root / ".lab").glob("*/evidence.json")).read_text()
            )
            self.assertFalse(record["passed"])
            self.assertEqual(len(record["metrics"]), 7)
            self.assertEqual(record["metrics"][0]["rejected_points"], 1)
            self.assertEqual(len(record["metrics"][0]["points"]), 1)
            self.assertEqual(record["metrics"][1]["collection_error"], "RuntimeError")
            self.assertTrue(record["metrics"][-1]["complete"])
            self.assertNotIn("private service detail", json.dumps(record))
            db.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
