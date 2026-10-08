#!/usr/bin/env python3
"""Two-million-row narrow-key sensitivity: isolate partition planning from payload I/O."""

import argparse
import collections
import datetime as dt
import json
import math
from pathlib import Path
import random
import statistics
import time
import uuid
from run_workload import Database, ROOT


def summary(values):
    ordered = sorted(values)
    return {
        "n": len(values),
        "min_ms": min(values),
        "median_ms": statistics.median(values),
        "p95_ms": ordered[math.ceil(0.95 * len(values)) - 1],
        "max_ms": max(values),
        "mean_ms": statistics.mean(values),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target", choices=["postgres:16", "postgres:17", "aurora"], required=True
    )
    parser.add_argument("--rows", type=int, choices=[10000, 2000000], default=2000000)
    parser.add_argument("--samples", type=int, choices=[5, 30, 100], default=30)
    args = parser.parse_args()
    dest = ROOT / ".lab" / ("queries-" + uuid.uuid4().hex[:12])
    dest.mkdir()
    record = {
        "passed": False,
        "target": args.target,
        "rows": args.rows,
        "samples": args.samples,
        "scope": "Narrow id/timestamp/UUID projection, not full payload read throughput",
        "results": [],
        "started_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    db = None
    started = time.monotonic()
    try:
        db = Database(args.target, "retention-query-" + uuid.uuid4().hex[:10], record)
        root = "query_" + uuid.uuid4().hex[:10]
        db.execute(f"CREATE SCHEMA {root}")
        record["engine"] = db.scalar("SELECT version()")
        ids = random.Random(29).sample(range(1, args.rows + 1), args.samples + 5)
        for partitions in [0, 10, 40, 120, 400]:
            table = f"{root}.event"
            suffix = " PARTITION BY RANGE(scheduled_at)" if partitions else ""
            db.execute(
                f"CREATE TABLE {table}(id bigint NOT NULL,scheduled_at timestamptz NOT NULL,external_ref_id uuid NOT NULL,PRIMARY KEY(id"
                + (",scheduled_at" if partitions else "")
                + "))"
                + suffix
            )
            if partitions:
                db.batch(
                    [
                        f"CREATE TABLE {root}.p{n} PARTITION OF {table} FOR VALUES FROM ('2025-01-01'::date+{n}) TO ('2025-01-01'::date+{n+1})"
                        for n in range(partitions)
                    ]
                )
            t = time.monotonic()
            for lo in range(1, args.rows + 1, 100000):
                db.execute(
                    f"INSERT INTO {table} SELECT n,'2025-01-01'::timestamptz+(n%{partitions or 40})*interval '1 day',md5(n::text)::uuid FROM generate_series({lo},{min(lo+99999,args.rows)}) n"
                )
            seed = time.monotonic() - t
            db.execute(f"ANALYZE {table}")
            for shape in [
                "id",
                "id_timestamp",
                "id_timestamp_expression",
                "external_no_index",
                "external_indexed",
            ]:
                if shape == "external_indexed":
                    db.execute(f"CREATE INDEX ON {table}(external_ref_id)")
                samples = []
                plans = []
                for iteration, ident in enumerate(ids):
                    predicate = f"id={ident}"
                    if shape == "id_timestamp":
                        stamp = (
                            dt.datetime(2025, 1, 1, tzinfo=dt.timezone.utc)
                            + dt.timedelta(days=ident % (partitions or 40))
                        ).isoformat()
                        predicate += f" AND scheduled_at='{stamp}'::timestamptz"
                    if shape == "id_timestamp_expression":
                        predicate += f" AND scheduled_at='2025-01-01'::timestamptz+({ident}%{partitions or 40})*interval '1 day'"
                    if shape.startswith("external_"):
                        predicate = f"external_ref_id=md5('{ident}')::uuid"
                    statement = f"SELECT id FROM {table} WHERE {predicate}"
                    t = time.perf_counter()
                    result = db.scalar(statement)
                    elapsed = (time.perf_counter() - t) * 1000
                    if result != ident:
                        raise RuntimeError("Lookup returned wrong identity")
                    if iteration >= 5:
                        samples.append(elapsed)
                    if iteration == len(ids) - 1:
                        explain = db.execute(
                            "EXPLAIN (ANALYZE,BUFFERS,WAL,TIMING OFF,FORMAT JSON) "
                            + statement
                        )[0]
                        plan = next(iter(explain.values()))
                        if isinstance(plan, str):
                            plan = json.loads(plan)
                        plans = plan
                nodes = collections.Counter()

                def visit(node):
                    nodes[node["Node Type"]] += 1
                    for child in node.get("Plans", []):
                        visit(child)

                visit(plans[0]["Plan"])
                record["results"].append(
                    {
                        "partitions": partitions,
                        "shape": shape,
                        "load_seconds": seed,
                        "client": summary(samples),
                        "plan": plans,
                        "node_counts": dict(nodes),
                    }
                )
                (dest / "evidence.json").write_text(
                    json.dumps(record, indent=2, default=str) + "\n"
                )
                print(
                    f"{args.target} partitions={partitions} {shape}: p95={summary(samples)['p95_ms']:.3f}ms",
                    flush=True,
                )
            db.execute(f"DROP TABLE {table} CASCADE")
        db.execute(f"DROP SCHEMA {root} CASCADE")
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
