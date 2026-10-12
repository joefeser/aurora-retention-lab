#!/usr/bin/env python3
"""Owned, synthetic Aurora 16 -> 17 upgrade benchmark. Raw diagnostics stay in .lab."""

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = ROOT / ".lab/upgrade-timing"
PUBLIC = ROOT / "evidence/upgrade-timing.json"
spec = importlib.util.spec_from_file_location(
    "upgrade_workload", ROOT / "benchmarks/workload.py"
)
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)
TIERS = {"1x": 32768, "4x": 131072, "16x": 524288}
CLASSES = {"serverless_v2": "db.serverless", "provisioned": "db.r6g.large"}
SCHEMA = "upgrade_bench"


def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(data, indent=2, default=str, allow_nan=False) + "\n"
    )
    temporary.replace(path)


def completed(cluster, writer, target, sql_version):
    """An initially available old engine or failed/rolled-back upgrade is not success."""
    return (
        cluster.get("Status") == "available"
        and cluster.get("EngineVersion") == target
        and writer.get("DBInstanceStatus") == "available"
        and writer.get("EngineVersion") == target
        and str(sql_version).startswith("17.")
    )


def expected_counts(parents):
    return dict(
        notification_template=1,
        notification_event=parents,
        delivery_queue=parents,
        push_delivery=parents * 3,
        send_history=parents // 4,
    )


def seed_statements(parents, chunk=1024):
    yield from w.schema_sql(SCHEMA)
    yield w.seed_functions(SCHEMA)
    yield f"INSERT INTO {SCHEMA}.notification_template(name) VALUES ('Synthetic upgrade benchmark')"
    for lo in range(1, parents + 1, chunk):
        hi = min(lo + chunk - 1, parents)
        yield w.seed_parent(SCHEMA, lo, hi, parents, "typical")
        # Use the existing projections unchanged; only bound their source rows.
        for statement in w.seed_children(SCHEMA, parents, "typical"):
            if f"FROM {SCHEMA}.notification_event p CROSS JOIN" in statement:
                statement += f" WHERE p.id BETWEEN {lo} AND {hi}"
            elif f"FROM {SCHEMA}.notification_event" in statement:
                statement += f" WHERE id BETWEEN {lo} AND {hi}"
            else:
                statement += f" AND id BETWEEN {lo} AND {hi}"
            yield statement
    for table in w.TABLES:
        yield f"ANALYZE {SCHEMA}.{table}"


def availability(probes):
    failed = [i for i, p in enumerate(probes) if not p["sql_ok"]]
    if not failed:
        return {
            "observed_failure_count": 0,
            "gap_upper_seconds": None,
            "note": "No failed samples does not prove zero downtime between probes.",
        }
    first, last = failed[0], failed[-1]
    previous = probes[first - 1] if first else None
    following = next((p for p in probes[last + 1 :] if p["sql_ok"]), None)
    return {
        "observed_failure_count": len(failed),
        "first_failure_seconds": probes[first]["elapsed_seconds"],
        "last_failure_seconds": probes[last]["elapsed_seconds"],
        "gap_upper_seconds": (
            following.get("response_elapsed_seconds", following["elapsed_seconds"])
            - previous["elapsed_seconds"]
            if following and previous
            else None
        ),
        "note": "Data API observation envelope; may include multiple outages and API unavailability. Not a wire-protocol outage measurement.",
    }


class Lab:
    def __init__(self, region, account):
        import boto3
        from botocore.config import Config

        self.region = region
        config = Config(
            connect_timeout=5, read_timeout=60, retries={"total_max_attempts": 1}
        )
        self.rds = boto3.client("rds", region_name=region, config=config)
        self.cf = boto3.client("cloudformation", region_name=region, config=config)
        self.data = boto3.client("rds-data", region_name=region, config=config)
        if (
            boto3.client(
                "sts", region_name=region, config=config
            ).get_caller_identity()["Account"]
            != account
        ):
            raise ValueError("Authenticated account differs from local lab inventory")

    def versions(self, source, target, instance):
        versions = [
            v
            for page in self.rds.get_paginator("describe_db_engine_versions").paginate(
                Engine="aurora-postgresql"
            )
            for v in page["DBEngineVersions"]
        ]
        ordinary = [
            v for v in versions if re.fullmatch(r"(16|17)\.[0-9]+", v["EngineVersion"])
        ]
        latest = max(
            (
                v["EngineVersion"]
                for v in ordinary
                if v["EngineVersion"].startswith("17.")
            ),
            key=lambda s: tuple(map(int, s.split("."))),
        )
        before = next(v for v in ordinary if v["EngineVersion"] == source)
        if (
            not source.startswith("16.")
            or target != latest
            or not any(
                t["EngineVersion"] == target and t["IsMajorVersionUpgrade"]
                for t in before["ValidUpgradeTarget"]
            )
        ):
            raise ValueError(
                "Require a supported direct PG16 -> latest regional PG17 upgrade"
            )
        for version in (source, target):
            if not self.rds.describe_orderable_db_instance_options(
                Engine="aurora-postgresql",
                EngineVersion=version,
                DBInstanceClass=instance,
            )["OrderableDBInstanceOptions"]:
                raise ValueError("Requested instance/version is not orderable")
        return latest

    def owned_stack(self, state):
        stack = self.cf.describe_stacks(
            StackName=state.get("stack_id", state["stack_name"])
        )["Stacks"][0]
        tags = {t["Key"]: t["Value"] for t in stack.get("Tags", [])}
        if (
            stack["StackName"] != state["stack_name"]
            or tags.get("UpgradeRun") != state["run_id"]
            or tags.get("Project") != "aurora-retention-lab"
        ):
            raise ValueError("Stack ownership mismatch")
        if state.get("stack_id") and state["stack_id"] != stack["StackId"]:
            raise ValueError("Stack identity mismatch")
        return stack

    def connect(self, state):
        stack = self.owned_stack(state)
        outputs = {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}
        state["outputs"] = outputs
        self.base = dict(
            resourceArn=outputs["UpgradeClusterArn"],
            secretArn=outputs["UpgradeSecretArn"],
            database="retentionlab",
        )
        cluster = self.rds.describe_db_clusters(
            DBClusterIdentifier=outputs["UpgradeClusterId"]
        )["DBClusters"][0]
        tags = {
            t["Key"]: t["Value"]
            for t in self.rds.list_tags_for_resource(
                ResourceName=cluster["DBClusterArn"]
            )["TagList"]
        }
        if (
            tags.get("Project") != "aurora-retention-lab"
            or tags.get("Purpose") != "pg16-upgrade-bench"
        ):
            raise ValueError("Cluster ownership mismatch")
        if (
            cluster["DBClusterArn"] != outputs["UpgradeClusterArn"]
            or len(cluster["DBClusterMembers"]) != 1
        ):
            raise ValueError("Expected exact single-writer owned cluster")
        state["cluster_id"] = cluster["DBClusterIdentifier"]
        state["writer_id"] = cluster["DBClusterMembers"][0]["DBInstanceIdentifier"]
        return cluster

    def sql(self, statement):
        args = dict(self.base, sql=statement, formatRecordsAs="JSON")
        if statement.lstrip().upper().startswith("SELECT"):
            return json.loads(
                self.data.execute_statement(**args).get("formattedRecords", "[]")
            )
        transaction = self.data.begin_transaction(**self.base)["transactionId"]
        try:
            self.data.execute_statement(
                **self.base,
                transactionId=transaction,
                sql="SET LOCAL statement_timeout='35s'",
            )
            response = self.data.execute_statement(**args, transactionId=transaction)
            self.data.commit_transaction(
                resourceArn=self.base["resourceArn"],
                secretArn=self.base["secretArn"],
                transactionId=transaction,
            )
            return json.loads(response.get("formattedRecords", "[]"))
        except BaseException:
            try:
                self.data.rollback_transaction(
                    resourceArn=self.base["resourceArn"],
                    secretArn=self.base["secretArn"],
                    transactionId=transaction,
                )
            except Exception:
                pass  # Preserve original/unknown commit outcome; never rerun the SQL.
            raise

    def scalar(self, statement):
        return next(iter(self.sql(statement)[0].values()))

    def measures(self):
        tables = {}
        for table in w.TABLES:
            tables[table] = self.sql(
                f"SELECT count(*)::bigint AS rows,pg_total_relation_size('{SCHEMA}.{table}')::bigint AS total_bytes,pg_relation_size('{SCHEMA}.{table}')::bigint AS heap_bytes,pg_indexes_size('{SCHEMA}.{table}')::bigint AS index_bytes FROM {SCHEMA}.{table}"
            )[0]
        return {
            "database_bytes": self.scalar(
                "SELECT pg_database_size(current_database())::bigint"
            ),
            "tables": tables,
            "server_version": self.scalar("SELECT current_setting('server_version')"),
            "version_string": self.scalar("SELECT version()"),
            "default_toast_compression": self.scalar(
                "SELECT current_setting('default_toast_compression')"
            ),
        }

    def states(self, state):
        cluster = self.rds.describe_db_clusters(
            DBClusterIdentifier=state["cluster_id"]
        )["DBClusters"][0]
        writer = self.rds.describe_db_instances(
            DBInstanceIdentifier=state["writer_id"]
        )["DBInstances"][0]
        return cluster, writer


def persist_receipt(directory, record):
    save(directory / "receipt.json", record)


def error_code(exc):
    return getattr(exc, "response", {}).get("Error", {}).get("Code", type(exc).__name__)


def cleanup(lab, directory, state):
    """Delete only the recorded run stack and its recorded upgrade snapshots."""
    stack = lab.owned_stack(state)
    if state.get("cluster_id"):
        snapshots = lab.rds.describe_db_cluster_snapshots(
            DBClusterIdentifier=state["cluster_id"]
        )["DBClusterSnapshots"]
        for snap in snapshots:
            # Unique cluster resource ownership, not just a name prefix.
            if snap["DBClusterIdentifier"] != state["cluster_id"]:
                raise ValueError("Snapshot ownership mismatch")
            if snap["SnapshotType"] == "manual":
                state.setdefault("snapshots", [])
                if snap["DBClusterSnapshotIdentifier"] not in state["snapshots"]:
                    state["snapshots"].append(snap["DBClusterSnapshotIdentifier"])
        save(directory / "inventory.json", state)
    if stack["StackStatus"] not in ("DELETE_COMPLETE", "DELETE_IN_PROGRESS"):
        lab.cf.delete_stack(StackName=state["stack_id"])
    deadline = time.monotonic() + 3600
    while lab.owned_stack(state)["StackStatus"] != "DELETE_COMPLETE":
        if time.monotonic() > deadline:
            raise TimeoutError("Stack deletion deadline; run cleanup explicitly later")
        time.sleep(30)
    for identifier in state.get("snapshots", []):
        try:
            snap = lab.rds.describe_db_cluster_snapshots(
                DBClusterSnapshotIdentifier=identifier
            )["DBClusterSnapshots"][0]
        except lab.rds.exceptions.DBClusterSnapshotNotFoundFault:
            continue
        if snap["DBClusterIdentifier"] != state["cluster_id"]:
            raise ValueError("Snapshot ownership mismatch")
        lab.rds.delete_db_cluster_snapshot(DBClusterSnapshotIdentifier=identifier)
    deadline = time.monotonic() + 900
    while (
        state.get("cluster_id")
        and lab.rds.describe_db_cluster_snapshots(
            DBClusterIdentifier=state["cluster_id"]
        )["DBClusterSnapshots"]
    ):
        if time.monotonic() > deadline:
            raise TimeoutError("Snapshot deletion deadline")
        time.sleep(30)
    state["cleanup"] = "verified_stack_deleted_no_cluster_snapshots"
    save(directory / "inventory.json", state)


def run(args):
    if not math.isfinite(args.prior_spend_usd) or args.prior_spend_usd < 0:
        raise ValueError("A finite conservative prior-spend estimate is required")
    old = json.loads((ROOT / ".lab/aws.json").read_text())
    lab = Lab(args.region, old["account"])
    lab.versions(args.source, args.target, CLASSES[args.config])
    PRIVATE.mkdir(parents=True, exist_ok=True)
    existing = [json.loads(p.read_text()) for p in PRIVATE.glob("*/inventory.json")]
    if any(
        s.get("cleanup") != "verified_stack_deleted_no_cluster_snapshots"
        for s in existing
    ):
        raise ValueError(
            "An earlier run is still retained; inspect/clean it before another run"
        )
    # Reservations are never refunded automatically based on delayed billing.
    reserved = sum(s.get("reserved_usd", 6.0) for s in existing)
    if args.prior_spend_usd + reserved + 6 > 50:
        raise ValueError(
            "Conservative six-dollar run reserve would exceed total $50 budget"
        )
    rid = uuid.uuid4().hex[:12]
    directory = PRIVATE / rid
    state = {
        "schema_version": 1,
        "run_id": rid,
        "stack_name": "aurora-upgrade-bench-" + rid,
        "account": old["account"],
        "region": args.region,
        "created_at": utc(),
        "reserved_usd": 6.0,
        "prior_spend_usd": args.prior_spend_usd,
        "cleanup": "retained",
    }
    record = {
        "schema_version": 1,
        "run_id": rid,
        "passed": False,
        "tier": args.tier,
        "config": args.config,
        "instance_class": CLASSES[args.config],
        "min_acu": 0.5 if args.config == "serverless_v2" else None,
        "max_acu": 2.0 if args.config == "serverless_v2" else None,
        "engine_requested_before": args.source,
        "engine_requested_after": args.target,
        "parent_rows": TIERS[args.tier],
        "profile": "typical",
        "errors": [],
        "warnings": [],
        "notes": "One run per case; physical sizes include normal TOAST and indexes. No padded/uncompressed size targets. Data API availability is not wire-protocol downtime.",
        "git_head": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "source_hashes": {
            p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest()
            for p in (
                "scripts/run-upgrade-timing.py",
                "benchmarks/workload.py",
                "infra/lab.json",
            )
        },
        "started_utc": utc(),
        "stage": "create",
        "probes": [],
    }
    save(directory / "inventory.json", state)
    persist_receipt(directory, record)
    print("Private run directory:", directory, flush=True)
    try:
        template = (ROOT / "infra/lab.json").read_text()
        lab.cf.validate_template(TemplateBody=template)
        result = lab.cf.create_stack(
            StackName=state["stack_name"],
            TemplateBody=template,
            Parameters=[
                {"ParameterKey": "LabMode", "ParameterValue": "upgrade"},
                {"ParameterKey": "UpgradeEngineVersion", "ParameterValue": args.source},
                {
                    "ParameterKey": "UpgradeInstanceClass",
                    "ParameterValue": CLASSES[args.config],
                },
            ],
            Tags=[
                {"Key": "Project", "Value": "aurora-retention-lab"},
                {"Key": "UpgradeRun", "Value": rid},
            ],
            Capabilities=["CAPABILITY_IAM"],
            TimeoutInMinutes=45,
        )
        state["stack_id"] = result["StackId"]
        save(directory / "inventory.json", state)
        deadline = time.monotonic() + 3600
        while True:
            stack = lab.owned_stack(state)
            if stack["StackStatus"] == "CREATE_COMPLETE":
                break
            if stack["StackStatus"] != "CREATE_IN_PROGRESS":
                raise RuntimeError("Stack creation failed")
            if time.monotonic() > deadline:
                raise TimeoutError("Stack creation deadline")
            time.sleep(30)
        lab.connect(state)
        save(directory / "inventory.json", state)
        cluster, writer = lab.states(state)
        if (
            cluster["EngineVersion"] != args.source
            or writer["DBInstanceClass"] != CLASSES[args.config]
        ):
            raise ValueError("Pre-load version/class mismatch")
        record["stage"] = "load"
        persist_receipt(directory, record)
        started = time.monotonic()
        for index, statement in enumerate(seed_statements(TIERS[args.tier])):
            if time.monotonic() - started > 7200:
                raise TimeoutError("Two-hour load guard")
            lab.sql(statement)
            if index % 40 == 0:
                print("Load statements:", index, flush=True)
        record["load_elapsed_seconds"] = time.monotonic() - started
        before = lab.measures()
        if {t: v["rows"] for t, v in before["tables"].items()} != expected_counts(
            TIERS[args.tier]
        ):
            raise ValueError("Pre-upgrade graph counts mismatch")
        record.update(
            before=before,
            size_before_bytes=before["database_bytes"],
            engine_before=cluster["EngineVersion"],
        )
        record["stage"] = "upgrade_request_outcome_unconfirmed"
        baseline_version = lab.scalar("SELECT current_setting('server_version')")
        record["upgrade_started_utc"] = utc()
        persist_receipt(directory, record)
        tick = time.monotonic()
        record["probes"].append(
            {
                "elapsed_seconds": 0.0,
                "sql_ok": True,
                "server_version": baseline_version,
                "cluster_status": cluster["Status"],
                "engine": cluster["EngineVersion"],
            }
        )
        command = [
            "aws",
            "--region",
            args.region,
            "--cli-connect-timeout",
            "5",
            "--cli-read-timeout",
            "60",
            "rds",
            "modify-db-cluster",
            "--db-cluster-identifier",
            state["cluster_id"],
            "--engine-version",
            args.target,
            "--allow-major-version-upgrade",
            "--apply-immediately",
            "--output",
            "json",
        ]
        with (directory / "modify.stdout.json").open("w") as out, (
            directory / "modify.stderr.txt"
        ).open("w") as err:
            modified = subprocess.run(command, stdout=out, stderr=err, timeout=70)
        if modified.returncode:
            raise RuntimeError("Upgrade CLI failed; raw output retained privately")
        record["stage"] = "upgrade_polling"
        persist_receipt(directory, record)
        while time.monotonic() - tick < 7200:
            cycle = time.monotonic()
            cluster, writer = lab.states(state)
            probe = {
                "elapsed_seconds": time.monotonic() - tick,
                "utc": utc(),
                "cluster_status": cluster["Status"],
                "writer_status": writer["DBInstanceStatus"],
                "engine": cluster["EngineVersion"],
                "sql_ok": False,
            }
            try:
                probe["server_version"] = lab.scalar(
                    "SELECT current_setting('server_version')"
                )
                probe["sql_ok"] = True
            except Exception as exc:
                probe["error_code"] = error_code(exc)
            probe["response_elapsed_seconds"] = time.monotonic() - tick
            record["probes"].append(probe)
            persist_receipt(directory, record)
            if time.monotonic() - tick >= 7200:
                raise TimeoutError("Two-hour upgrade deadline exceeded")
            if completed(cluster, writer, args.target, probe.get("server_version")):
                record["upgrade_elapsed_seconds"] = time.monotonic() - tick
                record["upgrade_finished_utc"] = utc()
                break
            if cluster["Status"] in (
                "upgrade-failed",
                "incompatible-parameters",
                "incompatible-restore",
                "inaccessible-encryption-credentials",
            ):
                raise RuntimeError("Terminal upgrade failure status")
            time.sleep(max(0, 30 - (time.monotonic() - cycle)))
        else:
            raise TimeoutError(
                "Upgrade did not reach verified PG17 availability within two hours; no retry"
            )
        record["availability"] = availability(record["probes"])
        record["stage"] = "post_upgrade_checks"
        persist_receipt(directory, record)
        after = lab.measures()
        if {t: v["rows"] for t, v in after["tables"].items()} != expected_counts(
            TIERS[args.tier]
        ):
            raise ValueError("Post-upgrade graph counts mismatch")
        record.update(
            after=after,
            size_after_bytes=after["database_bytes"],
            engine_after=cluster["EngineVersion"],
            smoke_parent_count=after["tables"]["notification_event"]["rows"],
            smoke_queue_count=after["tables"]["delivery_queue"]["rows"],
        )
        record["passed"] = True
        record["stage"] = "complete"
        persist_receipt(directory, record)
        print(
            "Upgrade verified:",
            record["upgrade_elapsed_seconds"],
            "seconds",
            flush=True,
        )
        if not args.keep:
            cleanup(lab, directory, state)
            record["cleanup"] = state["cleanup"]
            persist_receipt(directory, record)
    except BaseException as exc:
        if not record["passed"]:
            record["errors"].append(
                {
                    "type": type(exc).__name__,
                    "code": error_code(exc),
                    "stage": record["stage"],
                }
            )
        else:
            record["warnings"].append({"type": type(exc).__name__, "stage": "cleanup"})
        (directory / "failure.txt").write_text(str(exc) + "\n")
        record["availability"] = availability(record["probes"])
        record["finished_utc"] = utc()
        persist_receipt(directory, record)
        raise
    return directory


def publish(run_ids):
    if not run_ids or len(set(run_ids)) != len(run_ids):
        raise ValueError("Select distinct run IDs explicitly")
    runs = []
    for rid in run_ids:
        if not re.fullmatch("[a-f0-9]{12}", rid):
            raise ValueError("Invalid run ID")
        record = json.loads((PRIVATE / rid / "receipt.json").read_text())
        if record.get("run_id") != rid or type(record.get("passed")) is not bool:
            raise ValueError("Invalid receipt identity/outcome")
        if record.get("tier") not in TIERS or record.get("config") not in CLASSES:
            raise ValueError("Unknown case")
        if (
            record["parent_rows"] != TIERS[record["tier"]]
            or record["instance_class"] != CLASSES[record["config"]]
        ):
            raise ValueError("Case configuration mismatch")
        if record["passed"]:
            if (
                record["stage"] != "complete"
                or record["errors"]
                or not record["engine_before"].startswith("16.")
                or record["engine_after"] != record["engine_requested_after"]
            ):
                raise ValueError("Unverified upgrade outcome")
            seconds = record.get("upgrade_elapsed_seconds")
            if (
                type(seconds) not in (int, float)
                or not math.isfinite(seconds)
                or not 0 <= seconds <= 7200
            ):
                raise ValueError("Invalid upgrade duration")
            for phase in ("before", "after"):
                measured = record[phase]
                if {
                    t: v["rows"] for t, v in measured["tables"].items()
                } != expected_counts(record["parent_rows"]):
                    raise ValueError("Incomplete graph smoke check")
                if (
                    type(measured["database_bytes"]) is not int
                    or measured["database_bytes"] <= 0
                ):
                    raise ValueError("Missing physical size")
        elif not record.get("errors"):
            raise ValueError("Pending runs cannot be published as failures")
        allowed = set(
            "schema_version run_id passed tier config instance_class min_acu max_acu engine_requested_before engine_requested_after parent_rows profile errors warnings notes git_head source_hashes started_utc stage probes load_elapsed_seconds before size_before_bytes engine_before upgrade_started_utc upgrade_elapsed_seconds upgrade_finished_utc availability after size_after_bytes engine_after smoke_parent_count smoke_queue_count cleanup finished_utc".split()
        )
        if not set(record) <= allowed:
            raise ValueError("Unexpected receipt metadata")
        encoded = json.dumps(record, allow_nan=False)
        if re.search(
            r"arn:aws|[0-9]{12}:|/Users/|Password=|rds\.amazonaws\.com|secretArn|resourceArn|api_token|private_key|access_key",
            encoded,
            re.I,
        ):
            raise ValueError("Private metadata in receipt")
        runs.append(record)
    cases = {(r["tier"], r["config"]) for r in runs}
    save(
        PUBLIC,
        {
            "schema_version": 1,
            "scope": "Quiescent Aurora major upgrade; synthetic typical profile, one run per case. Data API availability only; no production downtime guarantee.",
            "runs": runs,
            "unmeasured_cases": [
                {"tier": t, "config": c, "status": "not_measured"}
                for c in CLASSES
                for t in TIERS
                if (t, c) not in cases
            ],
        },
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    execute = sub.add_parser("run")
    execute.add_argument("--tier", choices=TIERS, required=True)
    execute.add_argument("--config", choices=CLASSES, default="serverless_v2")
    execute.add_argument("--region", default="us-east-2")
    execute.add_argument("--source", required=True)
    execute.add_argument("--target", required=True)
    execute.add_argument("--prior-spend-usd", type=float, required=True)
    execute.add_argument(
        "--keep",
        action="store_true",
        help="Retain a successful test for explicit inspection; blocks the next run",
    )
    clean = sub.add_parser("cleanup")
    clean.add_argument("run_id")
    publication = sub.add_parser("publish")
    publication.add_argument("run_ids", nargs="+")
    args = parser.parse_args()
    if args.action == "run":
        run(args)
    elif args.action == "publish":
        publish(args.run_ids)
    else:
        if not re.fullmatch("[a-f0-9]{12}", args.run_id):
            raise ValueError("Invalid run ID")
        directory = PRIVATE / args.run_id
        state = json.loads((directory / "inventory.json").read_text())
        cleanup(Lab(state["region"], state["account"]), directory, state)


if __name__ == "__main__":
    main()
