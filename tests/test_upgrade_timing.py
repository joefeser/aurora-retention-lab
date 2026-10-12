import importlib.util
from pathlib import Path
import unittest
import json
import tempfile
from types import SimpleNamespace
from unittest.mock import patch, Mock

spec = importlib.util.spec_from_file_location(
    "upgrade_timing",
    Path(__file__).resolve().parents[1] / "scripts/run-upgrade-timing.py",
)
u = importlib.util.module_from_spec(spec)
spec.loader.exec_module(u)


class UpgradeTests(unittest.TestCase):
    def test_non_posix_can_import_publish_and_cleanup_but_run_fails_clearly(self):
        import builtins

        real_import = builtins.__import__

        def without_fcntl(name, *args, **kwargs):
            if name == "fcntl":
                raise ImportError("simulated non-POSIX platform")
            return real_import(name, *args, **kwargs)

        module = importlib.util.module_from_spec(spec)
        with patch("builtins.__import__", side_effect=without_fcntl):
            spec.loader.exec_module(module)
            self.assertTrue(callable(module.publish))
            self.assertTrue(callable(module.cleanup))
            with self.assertRaisesRegex(RuntimeError, "file locking"):
                module.run(SimpleNamespace())

    def test_missing_cli_stops_before_account_inventory_or_aws(self):
        with patch.object(u.shutil, "which", return_value=None), patch.object(u, "Lab") as lab:
            with self.assertRaisesRegex(ValueError, "AWS CLI"):
                u.run_locked(SimpleNamespace())
            lab.assert_not_called()

    def test_cleanup_rejects_cluster_from_another_stack_before_mutations(self):
        lab = Mock()
        lab.owned_stack.return_value = {
            "StackId": "owned-stack",
            "StackStatus": "CREATE_COMPLETE",
        }
        lab.cf_read.describe_stack_resources.return_value = {
            "StackResources": [
                {
                    "LogicalResourceId": "UpgradeCluster",
                    "PhysicalResourceId": "owned-cluster",
                }
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "does not match"):
                u.cleanup(
                    lab,
                    Path(directory),
                    {"stack_name": "owned", "cluster_id": "unrelated-cluster"},
                )
        lab.cf.delete_stack.assert_not_called()
        lab.rds.delete_db_cluster_snapshot.assert_not_called()

    def test_create_rejection_and_unknown_transport_outcome_are_distinct(self):
        with tempfile.TemporaryDirectory() as directory:
            for code, expected in [
                ("ValidationError", "rejected"),
                ("ReadTimeoutError", "pending"),
            ]:
                error = RuntimeError("fixture")
                error.response = {"Error": {"Code": code}}
                client = Mock()
                client.create_stack.side_effect = error
                state = {"create_outcome": "pending"}
                with self.assertRaises(RuntimeError):
                    u.create_stack_once(
                        client, Path(directory), state, StackName="owned-run"
                    )
                self.assertEqual(state["create_outcome"], expected)
                client.create_stack.assert_called_once_with(StackName="owned-run")

    def test_rollback_state_requires_progress_and_two_stable_sql_samples(self):
        old = dict(
            cluster_status="available",
            writer_status="available",
            engine="16.15",
            server_version="16.15",
            sql_ok=True,
        )
        progress = dict(
            cluster_status="upgrading",
            writer_status="upgrading",
            engine="16.15",
            sql_ok=False,
        )
        self.assertFalse(u.rollback_observed([old, old, old], "16.15", {}))
        self.assertFalse(u.rollback_observed([old, progress, old], "16.15", {}))
        self.assertFalse(
            u.rollback_observed(
                [progress, old, old], "16.15", {"EngineVersion": "17.11"}
            )
        )
        self.assertTrue(u.rollback_observed([progress, old, old], "16.15", {}))

    def test_success_publication_requires_sql_versions_sizes_and_unique_cases(self):
        import copy

        rid = "b" * 12
        counts = u.expected_counts(32768)
        base = dict(
            schema_version=1, profile="typical", git_head="a" * 40,
            source_hashes={"fixture": "b" * 64},
            started_utc="2026-10-12T00:00:00+00:00",
            finished_utc="2026-10-12T01:00:00+00:00",
            upgrade_finished_utc="2026-10-12T01:00:00+00:00",
            notes="synthetic fixture", warnings=[], probes=[],
            run_id=rid,
            passed=True,
            tier="1x",
            config="serverless_4acu",
            parent_rows=32768,
            instance_class="db.serverless",
            min_acu=4.0,
            max_acu=4.0,
            stage="complete",
            errors=[],
            engine_before="16.15",
            engine_requested_before="16.15",
            engine_after="17.11",
            engine_requested_after="17.11",
            upgrade_elapsed_seconds=600,
            size_before_bytes=10000,
            size_after_bytes=10000,
        )
        for phase, version in [("before", "16.15"), ("after", "17.11")]:
            base[phase] = dict(
                database_bytes=10000,
                server_version=version,
                tables={
                    t: dict(rows=n, total_bytes=1000, heap_bytes=500, index_bytes=200)
                    for t, n in counts.items()
                },
            )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / rid).mkdir()
            output = root / "public.json"
            with patch.object(u, "PRIVATE", root), patch.object(u, "PUBLIC", output):
                u.save(root / rid / "receipt.json", base)
                u.publish([rid])
                original = output.read_text()
                for mutation in (
                    "old_sql", "bad_size", "wrong_capacity", "future_schema",
                    "bad_errors", "missing_finish", "unknown_failure", "never_attempted",
                ):
                    bad = copy.deepcopy(base)
                    if mutation == "old_sql":
                        bad["after"]["server_version"] = "16.15"
                    elif mutation == "bad_size":
                        bad["size_after_bytes"] = 42
                    elif mutation == "wrong_capacity":
                        bad["max_acu"] = 2
                    elif mutation == "future_schema":
                        bad["schema_version"] = 2
                    elif mutation == "bad_errors":
                        bad["errors"] = "error"
                    elif mutation == "missing_finish":
                        del bad["upgrade_finished_utc"]
                    else:
                        bad.update(
                            passed=False,
                            stage="load" if mutation == "never_attempted" else "upgrade_polling",
                            errors=[{"type": "TimeoutError", "stage": "upgrade_polling"}],
                            upgrade_started_utc="2026-10-12T00:00:00+00:00",
                        )
                    u.save(root / rid / "receipt.json", bad)
                    with self.assertRaises(ValueError):
                        u.publish([rid])
                    self.assertEqual(output.read_text(), original)
                u.save(root / rid / "receipt.json", base)
                second = copy.deepcopy(base)
                second["run_id"] = "c" * 12
                u.save(root / second["run_id"] / "receipt.json", second)
                with self.assertRaisesRegex(ValueError, "one receipt"):
                    u.publish([rid, second["run_id"]])
                self.assertEqual(output.read_text(), original)
                # A confirmed rollback is publishable; a transport error alone is not.
                failure = copy.deepcopy(base)
                failure.update(
                    passed=False, stage="failed_rollback",
                    upgrade_started_utc="2026-10-12T00:00:00+00:00",
                    errors=[{"type": "AWSUpgradeFailure", "stage": "failed_rollback"}],
                )
                u.save(root / rid / "receipt.json", failure)
                u.publish([rid])
                self.assertFalse(json.loads(output.read_text())["runs"][0]["passed"])

    def test_cleanup_missing_stack_and_recovered_stack_id(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lab = Mock()
            lab.owned_stack.return_value = None
            state = {"stack_name": "owned-run", "create_outcome": "not_submitted"}
            u.cleanup(lab, root, state)
            lab.cf.delete_stack.assert_not_called()
            with self.assertRaisesRegex(ValueError, "uncertain"):
                u.cleanup(
                    lab, root, {"stack_name": "owned-run", "create_outcome": "pending"}
                )
            self.assertEqual(
                state["cleanup"], "verified_stack_deleted_no_cluster_snapshots"
            )
            state = {"stack_name": "owned-run", "create_outcome": "not_submitted"}
            lab.owned_stack.side_effect = [
                {"StackId": "owned-stack-id", "StackStatus": "CREATE_COMPLETE"},
                {"StackId": "owned-stack-id", "StackStatus": "DELETE_COMPLETE"},
            ]
            lab.cf_read.describe_stack_resources.return_value = {"StackResources": []}
            with patch.object(u.time, "sleep"):
                u.cleanup(lab, root, state)
            lab.cf.delete_stack.assert_called_once_with(StackName="owned-stack-id")
            self.assertEqual(
                json.loads((root / "inventory.json").read_text())["stack_id"],
                "owned-stack-id",
            )

    @unittest.skipUnless(importlib.util.find_spec("fcntl"), "POSIX locking test")
    def test_run_lock_blocks_second_launcher_before_budget_or_aws(self):
        import fcntl

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(u, "PRIVATE", root), patch.object(
                u, "run_locked"
            ) as launch:
                with (root / ".run.lock").open("a") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    with self.assertRaisesRegex(ValueError, "run lock"):
                        u.run(SimpleNamespace())
                    launch.assert_not_called()
                u.run(SimpleNamespace())
                launch.assert_called_once()

    def test_rollback_requires_current_attempt_failure_event(self):
        start = u.dt.datetime(2026, 10, 12, tzinfo=u.dt.timezone.utc)
        old = {
            "Date": start - u.dt.timedelta(seconds=1),
            "Message": "Database cluster is in a state that cannot be upgraded",
        }
        healthy = {"Date": start, "Message": "Upgrade preparation in progress"}
        failure = {
            "Date": start + u.dt.timedelta(seconds=300),
            "Message": old["Message"],
        }
        self.assertIsNone(u.failure_event([old, healthy], start))
        self.assertEqual(u.failure_event([old, healthy, failure], start), failure)

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
        for sql, (lo, hi) in zip(parents, [(1, 1024), (1025, 2048), (2049, 2050)]):
            self.assertIn(f"generate_series({lo},{hi})", sql)
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

    def test_publication_rejects_pending_and_incomplete_success_without_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rid = "a" * 12
            run = root / rid
            run.mkdir()
            output = root / "public.json"
            output.write_text("preserved")
            base = {
                "run_id": rid,
                "passed": False,
                "tier": "1x",
                "config": "serverless_v2",
                "parent_rows": 32768,
                "instance_class": "db.serverless",
                "errors": [],
                "min_acu": 0.5,
                "max_acu": 2.0,
            }
            with patch.object(u, "PRIVATE", root), patch.object(u, "PUBLIC", output):
                (run / "receipt.json").write_text(json.dumps(base))
                with self.assertRaises(ValueError):
                    u.publish([rid])
                self.assertEqual(output.read_text(), "preserved")
                base.update(
                    passed=True,
                    stage="complete",
                    engine_before="16.15",
                    engine_requested_before="16.15",
                    engine_after="17.11",
                    engine_requested_after="17.11",
                    upgrade_elapsed_seconds=float("inf"),
                )
                (run / "receipt.json").write_text(json.dumps(base))
                with self.assertRaises(ValueError):
                    u.publish([rid])
                self.assertEqual(output.read_text(), "preserved")

    def test_probe_envelope_includes_successful_response_latency(self):
        probes = [
            {"elapsed_seconds": 0, "sql_ok": True},
            {"elapsed_seconds": 30, "response_elapsed_seconds": 60, "sql_ok": False},
            {"elapsed_seconds": 90, "response_elapsed_seconds": 105, "sql_ok": True},
        ]
        self.assertEqual(u.availability(probes)["gap_upper_seconds"], 105)


if __name__ == "__main__":
    unittest.main()
