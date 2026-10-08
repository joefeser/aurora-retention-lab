"""Fault matrix for public evidence publication; no cloud, Docker or dotnet required."""

import copy
import csv
import gzip
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
from benchmarks import publish_report as report
from benchmarks.receipt_contract import benchmark_case, cloud_metric
from benchmarks.cloud_recovery import Inventory, recover
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.evidence = self.root / "evidence"
        self.out = self.root / "out"
        shutil.copytree(ROOT / "evidence/workload-report", self.evidence)
        shutil.copytree(ROOT / "docs/report", self.out)
        self.manifest = json.loads((self.evidence / "manifest.json").read_text())
        for attr, value in (("OUT", self.out), ("EVIDENCE", self.evidence)):
            p = patch.object(report, attr, value)
            p.start()
            self.addCleanup(p.stop)

    def receipt(self, label="pg17-dotnet"):
        return json.loads(
            gzip.decompress((self.evidence / (label + ".json.gz")).read_bytes())
        )

    def snapshot(self):
        return {
            str(p.relative_to(self.root)): p.read_bytes()
            for directory in (self.out, self.evidence)
            for p in directory.iterdir()
            if p.is_file()
        }

    def test_bdn_numeric_and_memory_fault_matrix_at_both_boundaries(self):
        good = self.receipt()
        for field, bad_values in (
            ("N", [None, "9", True, 0, -1, 1.5, float("inf")]),
            ("Mean", [None, "1", True, -1, float("nan"), float("inf")]),
            ("StandardDeviation", [None, "1", -1, float("inf")]),
        ):
            for value in bad_values:
                bad = copy.deepcopy(good)
                case = bad["benchmark_results"][0]
                case["Statistics"][field] = value
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValueError):
                        benchmark_case(case)
                    with self.assertRaises(ValueError):
                        report.validate("dotnet", bad)
        for value in (None, {}, {"BytesAllocatedPerOperation": 1}):
            bad = copy.deepcopy(good)
            bad["benchmark_results"][0]["Memory"] = value
            with self.assertRaises(ValueError):
                benchmark_case(bad["benchmark_results"][0])
            with self.assertRaises(ValueError):
                report.validate("dotnet", bad)
        for field in good["benchmark_results"][0]["Memory"]:
            bad = copy.deepcopy(good)
            bad["benchmark_results"][0]["Memory"][field] = "1"
            with self.assertRaises(ValueError):
                benchmark_case(bad["benchmark_results"][0])

    def test_entire_selection_validated_before_any_public_write(self):
        before = self.snapshot()
        for fault in ("memory", "late_conversion", "credential", "unknown"):
            bad = self.receipt()
            if fault == "memory":
                bad["benchmark_results"][0].pop("Memory")
            if fault == "late_conversion":
                bad["dataset"] = None
            if fault == "credential":
                bad["benchmark_results"][0]["api_token"] = "synthetic-secret"
            if fault == "unknown":
                bad["unexpected"] = "unapproved"
            # A conversion fault uses a required downstream field, not a validated identity.
            if fault == "late_conversion":
                bad = self.receipt("pg17-main")
                bad["profiles"][0]["notification_event"]["payload"][
                    "mean_bytes"
                ] = "bad-number"
            path = self.root / "bad.json"
            path.write_text(json.dumps(bad))
            entries = [
                self.manifest["runs"][0],
                {
                    "label": "bad",
                    "kind": "workload" if fault == "late_conversion" else "dotnet",
                    "source": str(path),
                },
            ]
            with self.subTest(fault=fault), self.assertRaises(ValueError):
                report.build(entries)
            self.assertEqual(before, self.snapshot())

    def test_nested_credentials_and_unknown_fields_fail_closed(self):
        for key in (
            "api_token",
            "ACCESS_KEY",
            "private_key",
            "Password",
            "secret",
            "credential",
            "unrecognized_metadata",
        ):
            data = self.receipt()
            data["benchmark_results"][0]["Statistics"][key] = "synthetic"
            with self.subTest(key=key), self.assertRaises(ValueError):
                report.validate("dotnet", data)

    def test_version_and_duplicate_selection_rejected_without_writes(self):
        before = self.snapshot()
        for version in (None, True, 0, 2, "1"):
            with self.assertRaises(ValueError):
                report.manifest_runs({"schema_version": version, "runs": []})
            with self.assertRaises(ValueError):
                report.validate("dotnet", self.receipt(), version)
        with self.assertRaises(ValueError):
            report.build([self.manifest["runs"][0]] * 2)
        self.assertEqual(before, self.snapshot())

    def test_subset_clears_stale_tables_and_charts_and_blocks_plot(self):
        entry = next(e for e in self.manifest["runs"] if e["kind"] == "dotnet")
        report.build([entry])
        for name in ("retirement-summary.csv", "query-matrix.csv", *report.CHARTS):
            self.assertFalse((self.out / name).exists(), name)
        self.assertTrue((self.out / "dotnet.csv").exists())
        with self.assertRaises(ValueError):
            report.verify_plot_inputs()

    def test_full_build_hash_binding_and_period_values(self):
        original_receipts = {p.name: p.read_bytes() for p in self.evidence.glob("*.gz")}
        report.build(self.manifest["runs"])
        report.verify_plot_inputs()
        self.assertEqual(
            original_receipts,
            {p.name: p.read_bytes() for p in self.evidence.glob("*.gz")},
        )
        with (self.out / "cloud-datapoints.csv").open() as stream:
            rows = list(csv.DictReader(stream))
        entry = next(
            e for e in self.manifest["runs"] if e["kind"] == "cloud-observation"
        )
        receipt = self.receipt(entry["label"])
        expected = [
            (m["name"], p["Timestamp"], p["Average"])
            for m in receipt["metrics"]
            for p in m["points"]
        ]
        self.assertEqual(
            [(r["metric"], r["timestamp"], float(r["average"])) for r in rows], expected
        )
        with (self.out / "query-matrix.csv").open("a") as stream:
            stream.write("stale\n")
        with self.assertRaises(ValueError):
            report.verify_plot_inputs()


class CollectionAndRecoveryTests(unittest.TestCase):
    def test_cloud_malformed_points_keep_valid_data_without_passing(self):
        good = {
            "Timestamp": "2026-10-08T15:00:00+00:00",
            "Unit": "Count",
            "Minimum": 1,
            "Average": 2,
            "Maximum": 3,
        }
        faults = [
            None,
            {},
            12,
            {**good, "Timestamp": "bad"},
            {**good, "Average": float("inf")},
            {**good, "Average": "2"},
        ]
        metric = cloud_metric("test", {"Datapoints": [good, *faults]})
        self.assertEqual(metric["points"], [good])
        self.assertFalse(metric["complete"])
        self.assertEqual(metric["rejected_points"], len(faults))
        self.assertTrue(cloud_metric("next", {"Datapoints": [good]})["complete"])
        self.assertFalse(cloud_metric("bad", {})["complete"])
        self.assertTrue(cloud_metric("unavailable", {"Datapoints": []})["complete"])

    def test_recovery_intent_is_durable_before_unacknowledged_ddl(self):
        with tempfile.TemporaryDirectory() as directory:
            inv = Inventory(
                directory, {"ClusterArn": "synthetic", "Bucket": "synthetic"}
            )
            sql = inv.prepare(
                ["CREATE SCHEMA owned", "ALTER SCHEMA owned RENAME TO owned_old"]
            )
            disk = json.loads(inv.path.read_text())
            self.assertEqual(disk["schemas"], ["owned", "owned_old"])
            self.assertIn("COMMENT ON SCHEMA owned IS 'retention-run:", sql[1])
            self.assertEqual(inv.archive_prefix(), "runs/recovery-" + inv.token + "/")
            self.assertEqual(
                json.loads(inv.path.read_text())["archive_prefix"], inv.archive_prefix()
            )

    def test_recovery_binding_ownership_errors_and_bounded_s3_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            outputs = {"ClusterArn": "synthetic", "Bucket": "synthetic"}
            inv = Inventory(directory, outputs)
            inv.prepare(["CREATE SCHEMA owned"])
            inv.archive_prefix()
            db = MagicMock(outputs=outputs)
            db.s3.get_bucket_versioning.return_value = {}
            db.s3.list_objects_v2.return_value = {
                "Contents": [{"Key": inv.archive_prefix() + "part"}]
            }
            db.s3.delete_objects.return_value = {"Errors": [{"Code": "denied"}]}
            result = recover(db, inv.path, execute=True)
            self.assertEqual(result["state"], "recovery_incomplete")
            self.assertEqual(result["cleanup_error"], "RuntimeError")
            self.assertIn("obj_description", db.execute.call_args.args[0])
            self.assertIn("retention-run:" + inv.token, db.execute.call_args.args[0])
            self.assertEqual(db.s3.delete_objects.call_count, 1)
            db.execute.side_effect = TimeoutError("private service detail")
            result = recover(db, inv.path, execute=True)
            self.assertEqual(result["cleanup_error"], "TimeoutError")
            self.assertNotIn("private service detail", inv.path.read_text())
            db.outputs = {**outputs, "Bucket": "other"}
            with self.assertRaises(ValueError):
                recover(db, inv.path, execute=True)

    def test_successful_recovery_verifies_empty_prefix_and_clears_prior_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            outputs = {"ClusterArn": "synthetic", "Bucket": "synthetic"}
            inv = Inventory(directory, outputs)
            prefix = inv.archive_prefix()
            inv.data["cleanup_error"] = "OldFailure"
            inv.save()
            db = MagicMock(outputs=outputs)
            db.s3.get_bucket_versioning.return_value = {}
            db.s3.list_objects_v2.side_effect = [
                {"Contents": [{"Key": prefix + "part"}]},
                {},
            ]
            db.s3.delete_objects.return_value = {}
            result = recover(db, inv.path, execute=True)
            self.assertEqual(result["state"], "recovered")
            self.assertIsNone(result["cleanup_error"])
            self.assertEqual(db.s3.list_objects_v2.call_count, 2)
            self.assertEqual(
                db.s3.delete_objects.call_args.kwargs["Delete"]["Objects"],
                [{"Key": prefix + "part"}],
            )

    def test_recovery_deadline_and_versioned_bucket_stop_before_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            outputs = {"ClusterArn": "synthetic", "Bucket": "synthetic"}
            inv = Inventory(directory, outputs)
            inv.prepare(["CREATE SCHEMA owned"])
            inv.archive_prefix()
            db = MagicMock(outputs=outputs)
            db.s3.get_bucket_versioning.return_value = {"Status": "Enabled"}
            self.assertEqual(
                recover(db, inv.path, execute=True)["cleanup_error"], "ValueError"
            )
            db.execute.assert_not_called()
            db.s3.delete_objects.assert_not_called()
            db.s3.get_bucket_versioning.return_value = {}
            with patch(
                "benchmarks.cloud_recovery.time.monotonic", side_effect=[0, 121, 121]
            ):
                self.assertEqual(
                    recover(db, inv.path, execute=True)["cleanup_error"], "TimeoutError"
                )
            db.execute.assert_not_called()
            db.s3.delete_objects.assert_not_called()

    def test_invalid_inventory_roots_leave_input_unchanged_and_do_not_call_services(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.json"
            for raw in ("[]", '"text"', "null", "true", "7", "1.5", "{"):
                for execute in (False, True):
                    with self.subTest(raw=raw, execute=execute):
                        path.write_text(raw)
                        db = MagicMock()
                        with self.assertRaisesRegex(
                            ValueError, "^Invalid recovery inventory$"
                        ):
                            recover(db, path, execute=execute)
                        self.assertEqual(path.read_text(), raw)
                        self.assertEqual(db.mock_calls, [])

    def test_archive_batch_ceiling_allows_final_confirmation_but_no_extra_delete(self):
        with tempfile.TemporaryDirectory() as directory:
            outputs = {"ClusterArn": "synthetic", "Bucket": "synthetic"}
            for batches in (0, 1, 9, 10, 11):
                with self.subTest(batches=batches):
                    inv = Inventory(directory, outputs)
                    prefix = inv.archive_prefix()
                    db = MagicMock(outputs=outputs)
                    db.s3.get_bucket_versioning.return_value = {}
                    page = {"Contents": [{"Key": prefix + "part"}], "IsTruncated": True}
                    db.s3.list_objects_v2.side_effect = [page] * batches + [
                        {"Contents": [], "IsTruncated": False}
                    ]
                    db.s3.delete_objects.return_value = {}
                    result = recover(db, inv.path, execute=True)
                    self.assertEqual(
                        result["state"],
                        "recovered" if batches <= 10 else "recovery_incomplete",
                    )
                    self.assertEqual(db.s3.delete_objects.call_count, min(batches, 10))
                    self.assertEqual(
                        db.s3.list_objects_v2.call_count, min(batches + 1, 11)
                    )
                    if batches > 10:
                        self.assertEqual(result["cleanup_error"], "TimeoutError")

    def test_last_confirmation_must_not_be_truncated_or_failed(self):
        with tempfile.TemporaryDirectory() as directory:
            outputs = {"ClusterArn": "synthetic", "Bucket": "synthetic"}
            for confirmation in (
                {"Contents": [], "IsTruncated": True},
                OSError("listing failed"),
            ):
                with self.subTest(confirmation=confirmation):
                    inv = Inventory(directory, outputs)
                    prefix = inv.archive_prefix()
                    db = MagicMock(outputs=outputs)
                    db.s3.get_bucket_versioning.return_value = {}
                    page = {"Contents": [{"Key": prefix + "part"}]}
                    db.s3.list_objects_v2.side_effect = [page] * 10 + [confirmation]
                    db.s3.delete_objects.return_value = {}
                    result = recover(db, inv.path, execute=True)
                    self.assertEqual(result["state"], "recovery_incomplete")
                    self.assertEqual(db.s3.delete_objects.call_count, 10)
                    self.assertEqual(db.s3.list_objects_v2.call_count, 11)

    def test_recovery_dry_run_and_prefix_validation_never_delete(self):
        with tempfile.TemporaryDirectory() as directory:
            outputs = {"ClusterArn": "synthetic", "Bucket": "synthetic"}
            inv = Inventory(directory, outputs)
            inv.prepare(["CREATE SCHEMA owned"])
            db = MagicMock(outputs=outputs)
            self.assertFalse(recover(db, inv.path)["execute"])
            db.execute.assert_not_called()
            db.s3.delete_objects.assert_not_called()
            inv.data["archive_prefix"] = "runs/"
            inv.save()
            with self.assertRaises(ValueError):
                recover(db, inv.path, execute=True)
            db.execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
