import copy
import gzip
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "publish_report", ROOT / "benchmarks/publish_report.py"
)
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)


class ReportEvidenceTests(unittest.TestCase):
    def receipt(self, label):
        return json.loads(
            gzip.decompress(
                (ROOT / "evidence/workload-report" / f"{label}.json.gz").read_bytes()
            )
        )

    def test_no_verified_success_from_partial_or_ambiguous_retirement(self):
        good = self.receipt("pg17-main")
        report.validate("workload", good)
        for change in (
            "root",
            "archive",
            "commit",
            "survivors",
            "missing_trial",
            "duplicate_trial",
            "nan",
        ):
            bad = copy.deepcopy(good)
            if change == "root":
                bad["passed"] = "true"
            elif change == "archive":
                bad["arms"][0]["archives"]["delivery_queue"]["verified"] = False
            elif change == "commit":
                bad["arms"][0][
                    "commit_state"
                ] = "retirement_started_outcome_unconfirmed"
            elif change == "survivors":
                bad["arms"][0]["survivors_verified"] = False
            elif change == "missing_trial":
                bad["arms"].pop()
            elif change == "duplicate_trial":
                bad["arms"][-1] = bad["arms"][0]
            elif change == "nan":
                bad["arms"][0]["retirement_server_seconds"] = float("nan")
            with self.subTest(change=change), self.assertRaises(ValueError):
                report.validate("workload", bad)

    def test_query_and_ef_case_duplication_cannot_hide_missing_case(self):
        for label, kind, key in [
            ("pg17-queries", "queries", "results"),
            ("pg17-dotnet", "dotnet", "benchmark_results"),
        ]:
            good = self.receipt(label)
            report.validate(kind, good)
            bad = copy.deepcopy(good)
            bad[key][-1] = bad[key][0]
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                report.validate(kind, bad)

    def test_export_rejects_private_host_and_infrastructure_metadata(self):
        for value in (
            "arn:aws:rds:example",
            "/Users/private/log",
            "Password=synthetic",
        ):
            bad = self.receipt("pg17-main")
            bad["unexpected"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                report.validate("workload", bad)

    def test_manifest_hash_binds_published_receipt(self):
        manifest = json.loads(
            (ROOT / "evidence/workload-report/manifest.json").read_text()
        )["runs"]
        entry = copy.deepcopy(next(x for x in manifest if x["label"] == "pg17-main"))
        entry["archive_sha256"] = "0" * 64
        with tempfile.TemporaryDirectory() as out, patch.object(
            report, "OUT", Path(out)
        ):
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                report.build([entry])

    def test_published_metadata_matches_supplied_column_contract(self):
        parent_required = {
            "id",
            "recipient_id",
            "subject_text",
            "scheduled_at",
            "notification_template_id",
            "created_at",
            "external_ref_id",
            "send_state",
            "remaining_minutes",
        }
        queue_required = {
            "id",
            "recipient_id",
            "notification_template_id",
            "notification_event_id",
            "subject_text",
            "send_details",
            "all_targets_attempted",
            "scheduled_at",
            "created_at",
            "send_state",
        }
        for label in ("pg17-main", "pg16-main", "aurora-main"):
            data = self.receipt(label)
            for profile in data["profiles"]:
                for table, required in [
                    ("notification_event", parent_required),
                    ("delivery_queue", queue_required),
                ]:
                    cols = {
                        c["column_name"]: c
                        for c in profile["columns"]
                        if c["table_name"] == table
                    }
                    self.assertEqual(len(cols), 17)
                    self.assertEqual(
                        {n for n, c in cols.items() if c["is_nullable"] == "NO"},
                        required,
                    )
                    self.assertEqual(
                        cols["content_body"]["character_maximum_length"], 10485760
                    )
                    self.assertEqual(
                        cols["recipient_id"]["character_maximum_length"], 10
                    )
                    self.assertEqual(
                        cols["subject_text"]["character_maximum_length"], 100
                    )
                    self.assertEqual(cols["metadata"]["data_type"], "jsonb")
                    self.assertEqual(
                        cols["scheduled_at"]["data_type"], "timestamp with time zone"
                    )
                parent = {
                    c["column_name"]: c
                    for c in profile["columns"]
                    if c["table_name"] == "notification_event"
                }
                queue = {
                    c["column_name"]: c
                    for c in profile["columns"]
                    if c["table_name"] == "delivery_queue"
                }
                self.assertEqual(
                    parent["message_text"]["character_maximum_length"], 1000
                )
                self.assertEqual(parent["external_ref_id"]["data_type"], "uuid")
                self.assertEqual(queue["message_text"]["data_type"], "text")
                self.assertEqual(queue["send_details"]["data_type"], "jsonb")

    def test_incomplete_cleanup_cannot_be_published(self):
        bad = self.receipt("pg17-main")
        bad["container_removed"] = False
        with self.assertRaises(ValueError):
            report.validate("workload", bad)


if __name__ == "__main__":
    unittest.main()
