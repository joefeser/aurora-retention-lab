#!/usr/bin/env python3
"""BenchmarkDotNet against full supplied-shape synthetic parent/queue rows, locally."""

import argparse
import json
import os
import subprocess
import time
import uuid
import workload as w
from run_workload import Database, ROOT, exact, profile


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--target", choices=["postgres:16", "postgres:17"], required=True)
    args = p.parse_args()
    dest = ROOT / ".lab" / ("dotnet-" + uuid.uuid4().hex[:12])
    dest.mkdir()
    record = {
        "passed": False,
        "target": args.target,
        "profile": "tail",
        "rows": 4096,
        "benchmark_results": [],
    }
    db = None
    started = time.monotonic()
    try:
        db = Database(args.target, "retention-dotnet-" + uuid.uuid4().hex[:10], record)
        record["engine"] = db.scalar("SELECT version()")
        db.batch(w.schema_sql("bench_baseline"))
        db.execute(w.seed_functions("bench_baseline"))
        db.execute(
            "INSERT INTO bench_baseline.notification_template(name) VALUES('Synthetic template')"
        )
        for lo in range(1, 4097, 128):
            db.execute(
                w.seed_parent("bench_baseline", lo, min(lo + 127, 4096), 4096, "tail")
            )
        db.batch(w.seed_children("bench_baseline", 4096, "tail"))
        db.batch(w.schema_sql("bench_partition", "partition"))
        db.batch(w.copy_sql("bench_baseline", "bench_partition", "partition"))
        db.batch(
            [
                f"ANALYZE {schema}.{t}"
                for schema in ("bench_baseline", "bench_partition")
                for t in w.TABLES
            ]
        )
        record["dataset"] = profile(db, "bench_baseline")
        for t in w.TABLES:
            db.execute(
                f"CREATE TABLE bench_baseline.snapshot_{t} AS TABLE bench_baseline.{t}"
            )
        info = db.connection.info
        # Only this invocation's generated password; never persisted in artifacts.
        password = db.connection.pgconn.password.decode()
        connection = f"Host=127.0.0.1;Port={info.port};Database=retention_perf;Username=postgres;Password={password};Timeout=5;Command Timeout=10"
        env = os.environ.copy()
        env["RETENTION_BENCH_CONNECTION"] = connection
        project = ROOT / "samples/Retention.Workload"
        subprocess.run(
            ["dotnet", "restore", str(project), "--locked-mode"],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
        with (dest / "run.log").open("w") as log:
            result = subprocess.run(
                [
                    "dotnet",
                    "run",
                    "--project",
                    str(project),
                    "-c",
                    "Release",
                    "--no-restore",
                    "--",
                    "--filter",
                    "*",
                    "--artifacts",
                    str(dest / "bdn"),
                ],
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=900,
            )
        text = (
            (dest / "run.log")
            .read_text()
            .replace(password, "[redacted]")
            .replace(connection, "[redacted connection]")
        )
        (dest / "run.log").write_text(text)
        if result.returncode:
            raise RuntimeError("BenchmarkDotNet failed; see ignored run.log")
        reports = list((dest / "bdn" / "results").glob("*-report-full.json"))
        if len(reports) != 1:
            raise RuntimeError("Expected one complete BenchmarkDotNet JSON report")
        report = json.loads(reports[0].read_text())
        for b in report["Benchmarks"]:
            stats = b.get("Statistics")
            if not stats or stats.get("N", 0) < 1:
                raise RuntimeError("Benchmark case did not produce measurements")
            record["benchmark_results"].append(
                {k: b.get(k) for k in ("Method", "Parameters", "Statistics", "Memory")}
            )
        if len(record["benchmark_results"]) != 24:
            raise RuntimeError("Expected all 24 benchmark cases")
        for schema, mode in [
            ("bench_baseline", "delete"),
            ("bench_partition", "partition"),
        ]:
            for t in w.TABLES:
                exact(
                    db,
                    w.logical_select(schema, t, mode),
                    f"TABLE bench_baseline.snapshot_{t}",
                )
        record["all_rows_unchanged"] = True
        record["passed"] = True
    except Exception as e:
        record["failure"] = {"type": type(e).__name__, "message": str(e)}
        raise
    finally:
        if db:
            db.close()
        record["elapsed_seconds"] = time.monotonic() - started
        (dest / "evidence.json").write_text(
            json.dumps(record, indent=2, default=str) + "\n"
        )
        print("Evidence:", dest / "evidence.json", flush=True)


if __name__ == "__main__":
    main()
