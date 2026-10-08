#!/usr/bin/env python3
"""Source-shaped, bounded retention benchmark. Local PG or the recorded private Aurora lab only."""

import argparse
import datetime as dt
import importlib.util
import json
import math
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
import uuid

import psycopg
from psycopg.rows import dict_row
import boto3
from botocore.config import Config
import workload as w

ROOT = Path(__file__).resolve().parents[1]


class Database:
    def __init__(self, target, name, record):
        self.created = False
        self.connection = None
        self.transaction = None
        self.name = name
        self.target = target
        self.record = record
        try:
            self._initialize(target, name, record)
        except BaseException:
            self.close()
            raise

    def _initialize(self, target, name, record):
        self.target = target
        self.name = name
        self.record = record
        self.transaction = None
        self.connection = None
        self.created = False
        self.started = time.monotonic()
        if target == "aurora":
            spec = importlib.util.spec_from_file_location(
                "lab", ROOT / "scripts/aws-lab.py"
            )
            lab = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(lab)
            state = lab.checked_state()
            self.region = state["region"]
            self.outputs = state["outputs"]
            if state["status"] not in ("CREATE_COMPLETE", "UPDATE_COMPLETE"):
                raise RuntimeError("Unstable lab")
            self.api = boto3.client(
                "rds-data",
                region_name=self.region,
                config=Config(read_timeout=60, retries={"total_max_attempts": 1}),
            )
            self.s3 = boto3.client("s3", region_name=self.region)
            self.rds = boto3.client("rds", region_name=self.region)
            cluster = self.rds.describe_db_clusters(
                DBClusterIdentifier=self.outputs["ClusterArn"]
            )["DBClusters"][0]
            capacity = cluster["ServerlessV2ScalingConfiguration"]
            if capacity["MaxCapacity"] > 1:
                raise RuntimeError("Existing 1 ACU ceiling required")
            self.record["capacity"] = capacity
            self.base = {
                "resourceArn": self.outputs["ClusterArn"],
                "secretArn": self.outputs["SecretArn"],
                "database": "retentionlab",
            }
        else:
            password = secrets.token_hex(24)
            env = os.environ.copy()
            env["POSTGRES_PASSWORD"] = password
            subprocess.run(
                [
                    "docker",
                    "run",
                    "-d",
                    "--pull",
                    "never",
                    "--name",
                    name,
                    "--label",
                    "project=aurora-retention-lab",
                    "--memory",
                    "4g",
                    "--cpus",
                    "2",
                    "--tmpfs",
                    "/var/lib/postgresql/data:rw,size=3g",
                    "-p",
                    "127.0.0.1::5432",
                    "-e",
                    "POSTGRES_PASSWORD",
                    "-e",
                    "POSTGRES_DB=retention_perf",
                    target,
                ],
                env=env,
                check=True,
                capture_output=True,
            )
            self.created = True
            for _ in range(90):
                if (
                    subprocess.run(
                        [
                            "docker",
                            "exec",
                            name,
                            "pg_isready",
                            "-h",
                            "127.0.0.1",
                            "-U",
                            "postgres",
                        ],
                        capture_output=True,
                    ).returncode
                    == 0
                ):
                    break
                time.sleep(0.5)
            else:
                raise RuntimeError("Startup deadline")
            port = subprocess.run(
                ["docker", "port", name, "5432/tcp"],
                check=True,
                text=True,
                capture_output=True,
            ).stdout.strip()
            if not port.startswith("127.0.0.1:"):
                raise RuntimeError("Not loopback bound")
            self.connection = psycopg.connect(
                host="127.0.0.1",
                port=int(port.split(":")[1]),
                user="postgres",
                password=password,
                dbname="retention_perf",
                autocommit=True,
                row_factory=dict_row,
            )
            self.record["image_id"] = subprocess.run(
                ["docker", "inspect", "--format", "{{.Image}}", name],
                check=True,
                text=True,
                capture_output=True,
            ).stdout.strip()
        self.execute("SET statement_timeout='35s'") if target != "aurora" else None
        self.wal_function = None
        for function in ("pg_current_wal_insert_lsn", "pg_current_wal_lsn"):
            try:
                if self.scalar(f"SELECT {function}()::text"):
                    self.wal_function = function
                    break
            except Exception:
                pass
        record["wal_counter"] = self.wal_function or "unavailable"

    def execute(self, sql):
        if time.monotonic() - self.started > 7200:
            raise RuntimeError("Two-hour run guard")
        if self.target == "aurora":
            args = dict(self.base, sql=sql, formatRecordsAs="JSON")
            if self.transaction:
                args["transactionId"] = self.transaction
            result = self.api.execute_statement(**args)
            return json.loads(result.get("formattedRecords", "[]"))
        with self.connection.cursor() as cursor:
            cursor.execute(sql)
            return cursor.fetchall() if cursor.description else []

    def scalar(self, sql):
        return next(iter(self.execute(sql)[0].values()))

    def batch(self, statements):
        # One API request, with the caller's existing transaction semantics.
        self.execute(
            "DO $batch$ BEGIN "
            + ";".join("EXECUTE '" + x.replace("'", "''") + "'" for x in statements)
            + "; END $batch$"
        )

    def begin(self):
        if self.transaction:
            raise RuntimeError("Already in transaction")
        if self.target == "aurora":
            self.transaction = self.api.begin_transaction(**self.base)["transactionId"]
        else:
            self.execute("BEGIN")
            self.transaction = True
        self.execute("SET LOCAL statement_timeout='35s'")
        self.execute("SET LOCAL lock_timeout='3s'")

    def commit(self):
        if self.target == "aurora":
            self.api.commit_transaction(
                resourceArn=self.base["resourceArn"],
                secretArn=self.base["secretArn"],
                transactionId=self.transaction,
            )
        else:
            self.execute("COMMIT")
        self.transaction = None

    def rollback(self):
        if not self.transaction:
            return
        if self.target == "aurora":
            self.api.rollback_transaction(
                resourceArn=self.base["resourceArn"],
                secretArn=self.base["secretArn"],
                transactionId=self.transaction,
            )
        else:
            self.execute("ROLLBACK")
        self.transaction = None

    def close(self):
        try:
            self.rollback()
        except Exception as exc:
            self.record["passed"] = False
            self.record["rollback_cleanup_error"] = type(exc).__name__
        finally:
            if self.connection:
                try:
                    self.connection.close()
                except Exception as exc:
                    self.record["passed"] = False
                    self.record["connection_cleanup_error"] = type(exc).__name__
            if self.created:
                try:
                    r = subprocess.run(
                        ["docker", "rm", "-f", self.name],
                        capture_output=True,
                        timeout=30,
                    )
                    self.record["container_removed"] = r.returncode == 0
                    if r.returncode:
                        self.record["passed"] = False
                except Exception as e:
                    self.record["container_removed"] = False
                    self.record["cleanup_error"] = type(e).__name__
                    self.record["passed"] = False


def exact(db, left, right):
    result = db.scalar(
        f"SELECT NOT EXISTS (({left} EXCEPT ALL {right}) UNION ALL ({right} EXCEPT ALL {left}))"
    )
    if result is not True:
        raise RuntimeError("Exact content comparison failed")


def profile(db, s):
    result = {}
    for table in w.TABLES:
        result[table] = {
            "rows": db.scalar(f"SELECT count(*) FROM {s}.{table}"),
            "storage_bytes": db.scalar(
                f"SELECT CASE WHEN (SELECT relkind FROM pg_class WHERE oid='{s}.{table}'::regclass)='p' THEN (SELECT coalesce(sum(pg_total_relation_size(relid)),0)::bigint FROM pg_partition_tree('{s}.{table}') WHERE isleaf) ELSE pg_total_relation_size('{s}.{table}') END"
            ),
        }
        if table in ("notification_event", "delivery_queue"):
            result[table]["payload"] = db.execute(
                f"""SELECT min(octet_length(content_body)) AS min_bytes,max(octet_length(content_body)) AS max_bytes,
              avg(octet_length(content_body)) AS mean_bytes,
              percentile_disc(.5) WITHIN GROUP(ORDER BY octet_length(content_body)) AS p50_bytes,
              percentile_disc(.95) WITHIN GROUP(ORDER BY octet_length(content_body)) AS p95_bytes,
              percentile_disc(.99) WITHIN GROUP(ORDER BY octet_length(content_body)) AS p99_bytes,
              count(*) FILTER(WHERE content_body IS NULL) AS nulls,count(*) FILTER(WHERE content_body='') AS empty_strings,
              sum(octet_length(content_body)) AS logical_body_bytes FROM {s}.{table}"""
            )[0]
    result["fanout"] = db.execute(
        f"""SELECT min(n) AS min_push, max(n) AS max_push,avg(n) AS mean_push,
        percentile_disc(.95) WITHIN GROUP(ORDER BY n) AS p95_push FROM (SELECT notification_event_id,count(*) n FROM {s}.push_delivery GROUP BY 1) x"""
    )[0]
    result["age_days"] = db.execute(
        f"SELECT scheduled_at::date AS scheduled_day,count(*) AS parents FROM {s}.notification_event GROUP BY 1 ORDER BY 1"
    )
    result["columns"] = db.execute(
        f"SELECT table_name,column_name,data_type,is_nullable,character_maximum_length FROM information_schema.columns WHERE table_schema='{s}' AND table_name IN ('notification_event','delivery_queue') ORDER BY table_name,ordinal_position"
    )
    return result


def timed(db, root, label, statements):
    label = label.replace("'", "''")
    tick = time.monotonic()
    wal_start = f"wal_start pg_lsn := {db.wal_function}();" if db.wal_function else ""
    wal_end = (
        f"pg_wal_lsn_diff({db.wal_function}(),wal_start)" if db.wal_function else "NULL"
    )
    db.execute(
        f"""DO $measure$ DECLARE started timestamptz:=clock_timestamp(); {wal_start} BEGIN
      {';'.join(statements)};
      INSERT INTO {root}.measurements(label,seconds,wal_bytes) VALUES('{label}',extract(epoch FROM clock_timestamp()-started),{wal_end}); END $measure$"""
    )
    client = time.monotonic() - tick
    return {
        **db.execute(
            f"SELECT seconds::double precision AS server_seconds,wal_bytes FROM {root}.measurements WHERE label='{label}' ORDER BY id DESC LIMIT 1"
        )[0],
        "client_seconds": client,
    }


def archive(db, s, mode, root, run, arm):
    """Archive each logical relation before destructive work; full restore comparison, not row counts."""
    result = {}
    cutoff = f"scheduled_at<{w.ANCHOR}"
    db.execute(f"CREATE SCHEMA {root}")
    for table in w.TABLES:
        logical = w.logical_select(s, table, mode)
        if table == "notification_event":
            query = f"SELECT * FROM ({logical}) x WHERE {cutoff}"
        elif table in ("delivery_queue", "push_delivery"):
            query = f"SELECT x.* FROM ({logical}) x JOIN {s}.notification_event p ON p.id=x.notification_event_id WHERE p.{cutoff}"
        elif table == "send_history":
            query = f"SELECT h.* FROM {s}.send_history h JOIN {s}.delivery_queue q ON q.id=h.delivery_queue_id JOIN {s}.notification_event p ON p.id=q.notification_event_id WHERE p.{cutoff}"
        else:
            query = logical  # Include reference template, unlike the known legacy archive gap.
        db.execute(f"CREATE TABLE {root}.{table} AS {query} WITH NO DATA")
        count = db.scalar(f"SELECT count(*) FROM ({query}) q")
        tick = time.monotonic()
        batches = []
        if db.target == "aurora":
            keycolumn = (
                "id"
                if table
                in ("notification_event", "delivery_queue", "notification_template")
                else (
                    "notification_event_id"
                    if table == "push_delivery"
                    else "delivery_queue_id"
                )
            )
            maxid = db.scalar(f"SELECT coalesce(max({keycolumn}),0) FROM ({query}) q")
            for lo in range(1, maxid + 1, 256):
                subset = f"SELECT * FROM ({query}) q WHERE {keycolumn} BETWEEN {lo} AND {lo+255} ORDER BY id"
                expected = db.scalar(f"SELECT count(*) FROM ({subset}) q")
                if not expected:
                    continue
                key = f"runs/{run}/workload/{arm}/{table}-{lo}.csv"
                uri = f"aws_commons.create_s3_uri('{db.outputs['Bucket']}','{key}','{db.region}')"
                t = time.monotonic()
                export = db.execute(
                    "SELECT * FROM aws_s3.query_export_to_s3('"
                    + subset.replace("'", "''")
                    + f"',{uri},options:='format csv')"
                )[0]
                if export["rows_uploaded"] != expected or export["files_uploaded"] != 1:
                    raise RuntimeError("Unexpected S3 export shape")
                metadata = db.s3.head_object(Bucket=db.outputs["Bucket"], Key=key)
                if metadata.get("ServerSideEncryption") != "aws:kms":
                    raise RuntimeError("Archive is not SSE-KMS")
                export_time = time.monotonic() - t
                t = time.monotonic()
                db.execute(
                    f"SELECT aws_s3.table_import_from_s3('{root}.{table}','','(format csv)',{uri})"
                )
                batches.append(
                    {
                        "rows": expected,
                        "bytes": export["bytes_uploaded"],
                        "export_seconds": export_time,
                        "import_seconds": time.monotonic() - t,
                        "encryption": "aws:kms",
                    }
                )
        else:
            # Actual binary COPY wire round-trip, using bounded chunks without caching the full relation in Python.
            with db.connection.cursor() as reader, db.connection.cursor() as writer:
                # libpq cannot interleave two COPY operations on one connection: bounded file staging.
                import tempfile

                with tempfile.TemporaryFile() as stream:
                    with reader.copy(
                        f"COPY ({query}) TO STDOUT (FORMAT BINARY)"
                    ) as copy:
                        for data in copy:
                            stream.write(data)
                    size = stream.tell()
                    stream.seek(0)
                    with writer.copy(
                        f"COPY {root}.{table} FROM STDIN (FORMAT BINARY)"
                    ) as copy:
                        while data := stream.read(1024 * 1024):
                            copy.write(data)
                batches.append(
                    {
                        "rows": count,
                        "bytes": size,
                        "transport": "binary COPY round-trip",
                    }
                )
        exact(db, query, f"TABLE {root}.{table}")
        if db.scalar(f"SELECT count(*) FROM {root}.{table}") != count:
            raise RuntimeError("Restore count mismatch")
        result[table] = {
            "rows": count,
            "verified": True,
            "elapsed_seconds": time.monotonic() - tick,
            "batches": batches,
        }
    return result


def run(args, record, dest):
    name = "retention-perf-" + uuid.uuid4().hex[:10]
    record["run_id"] = name
    db = None
    try:
        db = Database(args.target, name, record)
        root = "perf_" + uuid.uuid4().hex[:10]
        record["engine"] = db.scalar("SELECT version()")
        record["profiles"] = []
        record["arms"] = []
        record["settings"] = db.execute(
            "SELECT name,setting,unit FROM pg_settings WHERE name IN ('shared_buffers','work_mem','max_connections','wal_compression','synchronous_commit','random_page_cost','effective_cache_size') ORDER BY name"
        )
        db.batch(
            [
                f"CREATE SCHEMA {root}",
                f"CREATE TABLE {root}.measurements(id bigint GENERATED ALWAYS AS IDENTITY,label text,seconds double precision,wal_bytes bigint)",
            ]
        )
        if args.target == "aurora":
            db.execute("CREATE EXTENSION IF NOT EXISTS aws_s3 CASCADE")
        persist = lambda: (dest / "evidence.json").write_text(
            json.dumps(record, indent=2, default=str) + "\n"
        )
        for pname in args.profiles:
            src = root + "_" + pname
            db.batch(w.schema_sql(src))
            db.execute(w.seed_functions(src))
            db.execute(
                f"INSERT INTO {src}.notification_template(name) VALUES('Synthetic schedule template')"
            )
            t = time.monotonic()
            for lo in range(1, args.rows + 1, 128):
                db.execute(
                    w.seed_parent(src, lo, min(lo + 127, args.rows), args.rows, pname)
                )
            for statement in w.seed_children(src, args.rows, pname):
                db.execute(statement)
            db.batch([f"ANALYZE {src}.{table}" for table in w.TABLES])
            record["profiles"].append(
                {
                    "name": pname,
                    "generation_client_seconds": time.monotonic() - t,
                    "definition": w.PROFILES[pname],
                    **profile(db, src),
                }
            )
            persist()
            modes = args.modes
            for trial in range(args.trials):
                order = modes[trial % len(modes) :] + modes[: trial % len(modes)]
                if trial % 2:
                    order = list(reversed(order))
                for position, mode in enumerate(order):
                    s = f'{root}_a{len(record["arms"])}'
                    arc = s + "_archive"
                    arm = {
                        "profile": pname,
                        "mode": mode,
                        "trial": trial + 1,
                        "order": position + 1,
                        "batch_size": args.batch_size,
                        "passed": False,
                        "commit_state": "not_attempted",
                    }
                    record["arms"].append(arm)
                    persist()
                    db.batch(w.schema_sql(s, mode, indexed=not args.no_fk_index))
                    setup = timed(db, root, "setup-" + s, w.copy_sql(src, s, mode))
                    arm["copy_into_design"] = setup
                    arm["storage_before_bytes"] = db.scalar(
                        f"SELECT coalesce(sum(pg_total_relation_size(c.oid)),0)::bigint FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='{s}' AND c.relkind IN ('r','p')"
                    )
                    db.batch([f"ANALYZE {s}.{table}" for table in w.TABLES])
                    for table in w.TABLES:
                        exact(
                            db,
                            w.logical_select(src, table, "delete"),
                            w.logical_select(s, table, mode),
                        )
                    # Keep exact survivor copies outside the timed retirement section.
                    keep = s + "_keepers"
                    db.batch(w.schema_sql(keep, "delete"))
                    db.batch(
                        w.copy_sql(src, keep, "delete", f"scheduled_at>={w.ANCHOR}")
                    )
                    db.begin()
                    lockstart = time.monotonic()
                    db.execute(
                        "LOCK TABLE "
                        + ",".join(f"{s}.{table}" for table in w.TABLES)
                        + " IN ACCESS EXCLUSIVE MODE"
                    )
                    arm["lock_acquisition_client_seconds"] = (
                        time.monotonic() - lockstart
                    )
                    arm["stage"] = "archive"
                    persist()
                    arm["archives"] = archive(db, s, mode, arc, name, s)
                    if len(arm["archives"]) != 5 or not all(
                        x["verified"] is True for x in arm["archives"].values()
                    ):
                        raise RuntimeError("Unverified archive")
                    # Release after verified archive only in this strictly quiescent fixture: no writers exist.
                    db.commit()
                    arm["archive_transaction_client_seconds"] = (
                        time.monotonic() - lockstart
                    )
                    arm["stage"] = "retirement"
                    arm["commit_state"] = "retirement_started_outcome_unconfirmed"
                    persist()
                    retire_start = time.monotonic()
                    batches = []
                    if mode in ("delete", "split", "split_daily"):
                        if mode.startswith("split"):
                            db.begin()
                            drops = (
                                [
                                    f"DROP TABLE {s}.event_payload_d{day}"
                                    for day in range(45)
                                ]
                                if mode == "split_daily"
                                else [f"DROP TABLE {s}.event_payload_old"]
                            )
                            m = timed(db, root, "payload-" + s, drops)
                            db.commit()
                            arm["payload_drop"] = m
                        expired = args.rows * 4 // 5
                        for lo in range(1, expired + 1, args.batch_size):
                            hi = min(lo + args.batch_size - 1, expired)
                            db.begin()
                            m = timed(
                                db,
                                root,
                                f"batch-{s}-{lo}",
                                [
                                    f"DELETE FROM {s}.send_history h USING {s}.delivery_queue q,{s}.notification_event p WHERE h.delivery_queue_id=q.id AND q.notification_event_id=p.id AND p.id BETWEEN {lo} AND {hi} AND p.scheduled_at<{w.ANCHOR}",
                                    f"DELETE FROM {s}.notification_event WHERE id BETWEEN {lo} AND {hi} AND scheduled_at<{w.ANCHOR}",
                                ],
                            )
                            t = time.monotonic()
                            db.commit()
                            m["commit_client_seconds"] = time.monotonic() - t
                            m["parents"] = hi - lo + 1
                            batches.append(m)
                    elif mode == "partition":
                        db.begin()
                        operations = [
                            f"DELETE FROM {s}.send_history h USING {s}.delivery_queue q,{s}.notification_event p WHERE h.delivery_queue_id=q.id AND q.notification_event_id=p.id AND p.scheduled_at<{w.ANCHOR}"
                        ]
                        for table in (
                            "push_delivery",
                            "delivery_queue",
                            "notification_event",
                        ):
                            for day in range(45):
                                if table == "notification_event":
                                    operations.append(
                                        f"ALTER TABLE {s}.{table} DETACH PARTITION {s}.{table}_d{day}"
                                    )
                                operations.append(f"DROP TABLE {s}.{table}_d{day}")
                        batches.append(timed(db, root, "partition-" + s, operations))
                        db.commit()
                    else:
                        # Actual build-and-rename schema cutover, with full declared keys/FKs and identity state.
                        replacement = s + "_replacement"
                        db.begin()
                        statements = w.schema_sql(replacement, "delete") + w.copy_sql(
                            s, replacement, "delete", f"scheduled_at>={w.ANCHOR}"
                        )
                        for table in w.TABLES:
                            statements.append(
                                f"SELECT setval(pg_get_serial_sequence('{replacement}.{table}','id'),coalesce((SELECT max(id) FROM {replacement}.{table}),1),(SELECT count(*)>0 FROM {replacement}.{table}))"
                            )
                        # SELECT results must be consumed within the timing DO block.
                        statements = [
                            (
                                x.replace("SELECT setval", "PERFORM setval", 1)
                                if x.startswith("SELECT setval")
                                else x
                            )
                            for x in statements
                        ]
                        statements += [
                            f"ALTER SCHEMA {s} RENAME TO {s}_old",
                            f"ALTER SCHEMA {replacement} RENAME TO {s}",
                        ]
                        batches.append(timed(db, root, "copy-" + s, statements))
                        db.commit()
                        arm["old_copy_retained_until_verified"] = True
                    arm["retirement_client_seconds"] = time.monotonic() - retire_start
                    arm["retirement_batches"] = batches
                    arm["retirement_server_seconds"] = sum(
                        x["server_seconds"] for x in batches
                    ) + arm.get("payload_drop", {}).get("server_seconds", 0)
                    arm["commit_state"] = "committed_unverified"
                    persist()
                    for table in w.TABLES:
                        exact(
                            db,
                            w.logical_select(
                                s, table, mode if mode != "copy" else "delete"
                            ),
                            w.logical_select(keep, table, "delete"),
                        )
                    arm["survivors_verified"] = True
                    arm["storage_after_bytes"] = db.scalar(
                        f"SELECT coalesce(sum(pg_total_relation_size(c.oid)),0)::bigint FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='{s}' AND c.relkind IN ('r','p')"
                    )
                    arm["passed"] = True
                    arm["commit_state"] = "committed_verified"
                    arm["stage"] = "complete"
                    persist()
                    print(
                        f"PASS {args.target} {pname} trial {trial+1} {mode}: {arm['retirement_server_seconds']:.3f}s server / {arm['retirement_client_seconds']:.3f}s client",
                        flush=True,
                    )
                    cleanup = [s, keep, arc] + ([s + "_old"] if mode == "copy" else [])
                    # All are exact random schemas created by this run; the source profile remains for other arms.
                    db.batch([f"DROP SCHEMA {x} CASCADE" for x in cleanup])
            db.execute(f"DROP SCHEMA {src} CASCADE")
        record["passed"] = True
    except Exception as exc:
        record["passed"] = False
        record["failure"] = {"type": type(exc).__name__, "message": str(exc)}
        raise
    finally:
        if db:
            db.close()
        record["elapsed_seconds"] = round(
            time.monotonic() - record["started_monotonic"], 3
        )
        record.pop("started_monotonic", None)
        (dest / "evidence.json").write_text(
            json.dumps(record, indent=2, default=str) + "\n"
        )
        print("Evidence:", dest / "evidence.json", flush=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--target", choices=["postgres:16", "postgres:17", "aurora"], required=True
    )
    p.add_argument(
        "--rows", type=int, choices=[128, 1024, 4096, 16384, 32768], default=4096
    )
    p.add_argument("--trials", type=int, choices=[1, 3], default=3)
    p.add_argument(
        "--profiles", nargs="+", choices=list(w.PROFILES), default=list(w.PROFILES)
    )
    p.add_argument(
        "--modes",
        nargs="+",
        choices=["delete", "partition", "split", "split_daily", "copy"],
        default=["delete", "partition", "split_daily", "copy"],
    )
    p.add_argument(
        "--no-fk-index",
        action="store_true",
        help="Sensitivity case only: omit supporting child FK indexes",
    )
    p.add_argument("--batch-size", type=int, choices=[64, 256, 1024], default=256)
    args = p.parse_args()
    dest = ROOT / ".lab" / ("workload-" + uuid.uuid4().hex[:12])
    dest.mkdir(parents=True)
    record = {
        "passed": False,
        "target": args.target,
        "parent_rows": args.rows,
        "expired_fraction": 0.8,
        "trials_per_design": args.trials,
        "supporting_fk_indexes": not args.no_fk_index,
        "batch_size": args.batch_size,
        "started_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "started_monotonic": time.monotonic(),
        "scope": "Source-shaped synthetic data; quiescent archive then retirement; no production speed or compliance claim",
    }
    run(args, record, dest)


if __name__ == "__main__":
    main()
