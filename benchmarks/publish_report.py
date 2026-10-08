#!/usr/bin/env python3
"""Publish explicitly selected receipts and derive every report table from them."""

import argparse
import csv
import gzip
import hashlib
import json
import math
from pathlib import Path
import re
import statistics

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/report"
EVIDENCE = ROOT / "evidence/workload-report"
TABLES = (
    "notification_event",
    "delivery_queue",
    "push_delivery",
    "send_history",
    "notification_template",
)


def validate(kind, data):
    if data.get("passed") is not True:
        raise ValueError("Only completed passing receipts can support this report")
    if data.get("target") not in ("postgres:16", "postgres:17", "aurora"):
        raise ValueError("Unknown engine target")
    if data["target"] != "aurora" and data.get("container_removed") is not True:
        raise ValueError("Local cleanup not confirmed")
    if kind == "workload":
        if not data.get("arms"):
            raise ValueError("Missing retirement matrix")
        modes = {a["mode"] for a in data["arms"]}
        expected = {
            (p["name"], m, t)
            for p in data["profiles"]
            for m in modes
            for t in range(1, data["trials_per_design"] + 1)
        }
        if (
            len(data["arms"]) != len(expected)
            or {(a["profile"], a["mode"], a["trial"]) for a in data["arms"]} != expected
        ):
            raise ValueError("Incomplete or duplicate retirement trial")
        for arm in data["arms"]:
            if (
                arm.get("passed") is not True
                or arm.get("survivors_verified") is not True
                or arm.get("commit_state") != "committed_verified"
            ):
                raise ValueError("Retirement outcome not verified")
            archives = arm.get("archives", {})
            if set(archives) != set(TABLES) or any(
                a.get("verified") is not True for a in archives.values()
            ):
                raise ValueError("Archive relation missing or unverified")
            if any(
                not math.isfinite(arm[k]) or arm[k] < 0
                for k in ("retirement_server_seconds", "retirement_client_seconds")
            ):
                raise ValueError("Invalid duration")
    elif kind == "queries":
        expected = {
            (n, s)
            for n in (0, 10, 40, 120, 400)
            for s in (
                "id",
                "id_timestamp",
                "id_timestamp_expression",
                "external_no_index",
                "external_indexed",
            )
        }
        results = data.get("results", [])
        if (
            len(results) != 25
            or {(r["partitions"], r["shape"]) for r in results} != expected
        ):
            raise ValueError("Incomplete query matrix")
        if any(r["client"]["n"] != data["samples"] or not r["plan"] for r in results):
            raise ValueError("Missing query samples/plans")
    elif kind == "dotnet":
        results = data.get("benchmark_results", [])
        methods = (
            "ReadParentById",
            "ReadParentQualified",
            "ReadParentByExternalReference",
            "ReadGraphSingleQuery",
            "ReadGraphSplitQuery",
            "RescheduleTrackedQueueRollback",
        )
        expected = {
            (m, f"Partitioned={p}&AutoPrepare={a}")
            for m in methods
            for p in ("False", "True")
            for a in ("False", "True")
        }
        if (
            len(results) != 24
            or {(r["Method"], r["Parameters"]) for r in results} != expected
            or data.get("all_rows_unchanged") is not True
        ):
            raise ValueError("Incomplete EF benchmark or changed rows")
    elif kind == "migration":
        if len(data.get("backfills", [])) != 3 or any(
            b.get("passed") is not True for b in data["backfills"]
        ):
            raise ValueError("Missing backfill trial")
        if (
            data["target"] != "aurora"
            and data.get("concurrent_move", {}).get("retry_new_transaction_passed")
            is not True
        ):
            raise ValueError("Missing retry evidence")
    elif kind == "cloud-observation":
        if not data.get("metrics") or not any(
            m.get("available") is True for m in data["metrics"]
        ):
            raise ValueError("No cloud metrics available")
    elif kind == "fidelity":
        if len(data.get("checks", [])) != 3 or set(data.get("archives", {})) != set(
            TABLES
        ):
            raise ValueError("Missing archival boundary checks")
    else:
        raise ValueError("Unknown evidence kind")
    encoded = json.dumps(data)
    if re.search(
        r"arn:aws|account[_-]?id|/Users/|rds\.amazonaws\.com|Password=|secretArn|resourceArn",
        encoded,
        re.I,
    ):
        raise ValueError("Private infrastructure or host metadata in evidence")


def write_table(name, rows):
    if not rows:
        return
    with (OUT / (name + ".csv")).open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def fmt(v):
    if v is None:
        return "unavailable"
    if isinstance(v, float):
        return f"{v:.3f}"
    return str(v)


def markdown(headers, rows):
    return "\n".join(
        [
            "| " + " | ".join(headers) + " |",
            "| " + " | ".join(["---"] * len(headers)) + " |",
        ]
        + ["| " + " | ".join(fmt(v) for v in row) + " |" for row in rows]
    )


def build(entries):
    OUT.mkdir(exist_ok=True)
    EVIDENCE.mkdir(exist_ok=True)
    manifest = []
    ret = []
    datasets = []
    queries = []
    bdn = []
    migration = []
    scheduling = []
    fidelity = []
    cloud = []
    for entry in entries:
        label, kind = entry["label"], entry["kind"]
        if not re.fullmatch("[a-z0-9-]+", label):
            raise ValueError("Invalid report label")
        if "source" in entry:
            source = ROOT / entry["source"]
            raw = source.read_bytes()
            data = json.loads(raw)
        else:
            packed = (EVIDENCE / entry["file"]).read_bytes()
            if hashlib.sha256(packed).hexdigest() != entry["archive_sha256"]:
                raise ValueError("Compressed receipt hash mismatch")
            raw = gzip.decompress(packed)
            if hashlib.sha256(raw).hexdigest() != entry["receipt_sha256"]:
                raise ValueError("Receipt hash mismatch")
            data = json.loads(raw)
        validate(kind, data)
        packed = gzip.compress(raw, mtime=0)
        file = label + ".json.gz"
        (EVIDENCE / file).write_bytes(packed)
        manifest.append(
            {
                "label": label,
                "kind": kind,
                "file": file,
                "target": data["target"],
                "receipt_sha256": hashlib.sha256(raw).hexdigest(),
                "archive_sha256": hashlib.sha256(packed).hexdigest(),
            }
        )
        base = {"evidence": label, "target": data["target"]}
        if kind == "workload":
            for p in data["profiles"]:
                for t in ("notification_event", "delivery_queue"):
                    row = {
                        **base,
                        "parent_rows": data["parent_rows"],
                        "profile": p["name"],
                        "table": t,
                        "rows": p[t]["rows"],
                        "storage_bytes": p[t]["storage_bytes"],
                    }
                    row.update(
                        {
                            k: float(v) if k == "mean_bytes" else v
                            for k, v in p[t]["payload"].items()
                        }
                    )
                    datasets.append(row)
            for a in data["arms"]:
                measures = a["retirement_batches"] + (
                    [a["payload_drop"]] if "payload_drop" in a else []
                )
                wal = (
                    None
                    if any(x.get("wal_bytes") is None for x in measures)
                    else sum(int(x["wal_bytes"]) for x in measures)
                )
                ret.append(
                    {
                        **base,
                        "parent_rows": data["parent_rows"],
                        "profile": a["profile"],
                        "mode": a["mode"],
                        "batch_size": a["batch_size"],
                        "fk_indexes": data.get("supporting_fk_indexes", True),
                        "trial": a["trial"],
                        "server_seconds": a["retirement_server_seconds"],
                        "client_seconds": a["retirement_client_seconds"],
                        "archive_transaction_client_seconds": a[
                            "archive_transaction_client_seconds"
                        ],
                        "archive_bytes": sum(
                            b["bytes"]
                            for t in a["archives"].values()
                            for b in t["batches"]
                        ),
                        "retirement_statement_wal_bytes": wal,
                        "before_bytes": a["storage_before_bytes"],
                        "after_bytes": a["storage_after_bytes"],
                        "copy_into_design_server_seconds": a["copy_into_design"][
                            "server_seconds"
                        ],
                    }
                )
        elif kind == "queries":
            for r in data["results"]:
                plan = r["plan"][0]
                top = plan["Plan"]
                queries.append(
                    {
                        **base,
                        "rows": data["rows"],
                        "partitions": r["partitions"],
                        "shape": r["shape"],
                        **r["client"],
                        "planning_ms": plan["Planning Time"],
                        "execution_ms": plan["Execution Time"],
                        "shared_hit_blocks": top.get("Shared Hit Blocks", 0),
                        "shared_read_blocks": top.get("Shared Read Blocks", 0),
                        "plan_nodes": sum(r["node_counts"].values()),
                    }
                )
        elif kind == "dotnet":
            for b in data["benchmark_results"]:
                s = b["Statistics"]
                bdn.append(
                    {
                        **base,
                        "method": b["Method"],
                        "parameters": b["Parameters"],
                        "mean_ms": s["Mean"] / 1e6,
                        "stddev_ms": s["StandardDeviation"] / 1e6,
                        "retained_iterations": s["N"],
                        "allocated_bytes_per_operation": b["Memory"][
                            "BytesAllocatedPerOperation"
                        ],
                    }
                )
        elif kind == "migration":
            for b in data["backfills"]:
                values = [x["wal_bytes"] for x in b["batches"]]
                migration.append(
                    {
                        **base,
                        "trial": b["trial"],
                        "child_rows": b["child_rows"],
                        "add_columns_server_seconds": b["add_columns"][
                            "server_seconds"
                        ],
                        "backfill_server_seconds": sum(
                            x["server_seconds"] for x in b["batches"]
                        ),
                        "backfill_statement_wal_bytes": (
                            None
                            if any(x is None for x in values)
                            else sum(int(x) for x in values)
                        ),
                        "constraints_server_seconds": b["constraints"][
                            "server_seconds"
                        ],
                    }
                )
            for s in data["scheduling"]:
                scheduling.append({**base, **s})
        elif kind == "cloud-observation":
            for metric in data["metrics"]:
                points = metric["points"]
                cloud.append(
                    {
                        **base,
                        "metric": metric["name"],
                        "available": bool(points),
                        "points": len(points),
                        "unit": points[0]["Unit"] if points else "unavailable",
                        "minimum": (
                            min(p["Minimum"] for p in points) if points else None
                        ),
                        "mean_of_period_averages": (
                            statistics.mean(p["Average"] for p in points)
                            if points
                            else None
                        ),
                        "maximum": (
                            max(p["Maximum"] for p in points) if points else None
                        ),
                        "window_start_utc": data["window_start_utc"],
                        "window_end_utc": data["window_end_utc"],
                    }
                )
        elif kind == "fidelity":
            fidelity.append(
                {
                    **base,
                    "checks_passed": len(data["checks"]),
                    "relations_verified": len(data["archives"]),
                    "archive_bytes": sum(
                        b["bytes"]
                        for t in data["archives"].values()
                        for b in t["batches"]
                    ),
                }
            )
    for name, rows in [
        ("retirement-trials", ret),
        ("dataset-profiles", datasets),
        ("query-matrix", queries),
        ("dotnet", bdn),
        ("migration", migration),
        ("scheduling", scheduling),
        ("fidelity", fidelity),
        ("cloud-observation", cloud),
    ]:
        write_table(name, rows)
    groups = {}
    for r in ret:
        key = tuple(
            r[k]
            for k in (
                "target",
                "parent_rows",
                "profile",
                "mode",
                "batch_size",
                "fk_indexes",
            )
        )
        groups.setdefault(key, []).append(r)
    aggregate = []
    for key, values in sorted(groups.items()):
        item = dict(
            zip(
                (
                    "target",
                    "parent_rows",
                    "profile",
                    "mode",
                    "batch_size",
                    "fk_indexes",
                ),
                key,
            )
        )
        item["trials"] = len(values)
        for field in (
            "server_seconds",
            "client_seconds",
            "archive_transaction_client_seconds",
            "before_bytes",
            "after_bytes",
            "retirement_statement_wal_bytes",
        ):
            vals = [r[field] for r in values]
            item[field + "_median"] = (
                None if any(x is None for x in vals) else statistics.median(vals)
            )
        item["server_seconds_min"] = min(v["server_seconds"] for v in values)
        item["server_seconds_max"] = max(v["server_seconds"] for v in values)
        aggregate.append(item)
    write_table("retirement-summary", aggregate)
    (EVIDENCE / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "description": "Explicit successful experiment receipts; source attachments, credentials and raw host reports excluded. SHA256 binds bytes, not runtime attestation.",
                "runs": manifest,
            },
            indent=2,
        )
        + "\n"
    )
    sections = [
        "# Complete performance tables",
        "Generated by `python3 benchmarks/publish_report.py`. All values derive from the [receipt manifest](../../evidence/workload-report/manifest.json). Durations are seconds unless labeled ms. Raw CSV tables preserve precision.",
        "## Retirement: medians and observed range",
        "Three trials per configuration; server excludes archive and commit latency. Client retirement includes transaction/RPC/measurement overhead. Split aggregate has one historical payload leaf; split_daily has 45. Composite partition retires 135 expired leaves.",
    ]
    sections.append(
        markdown(
            [
                "Engine",
                "Parents",
                "Profile",
                "Design",
                "Batch",
                "FK indexes",
                "n",
                "Server median s",
                "Server min–max s",
                "Client median s",
                "Archive transaction median s",
            ],
            [
                [
                    r["target"],
                    r["parent_rows"],
                    r["profile"],
                    r["mode"],
                    r["batch_size"],
                    r["fk_indexes"],
                    r["trials"],
                    r["server_seconds_median"],
                    f"{r['server_seconds_min']:.3f}–{r['server_seconds_max']:.3f}",
                    r["client_seconds_median"],
                    r["archive_transaction_client_seconds_median"],
                ]
                for r in aggregate
            ],
        )
    )
    sections.extend(
        [
            "[Every retirement trial](retirement-trials.csv) · [summary including storage and WAL](retirement-summary.csv)",
            "## Dataset: original baseline physical size and logical HTML",
            "Mean excludes NULL but includes empty text. Physical size includes indexes and TOAST. Profile rows repeat across independently generated sensitivity runs in the CSV; the summary below shows main baseline profiles only.",
        ]
    )
    unique = {}
    for d in datasets:
        key = (d["target"], d["parent_rows"], d["profile"], d["table"])
        unique.setdefault(key, d)
    sections.append(
        markdown(
            [
                "Engine",
                "Parents",
                "Profile",
                "Table",
                "Physical MiB",
                "Mean HTML KiB",
                "p50 KiB",
                "p95 KiB",
                "p99 KiB",
                "Max KiB",
                "NULL",
                "Empty",
            ],
            [
                [
                    d["target"],
                    d["parent_rows"],
                    d["profile"],
                    d["table"],
                    d["storage_bytes"] / 2**20,
                    d["mean_bytes"] / 1024,
                    d["p50_bytes"] / 1024,
                    d["p95_bytes"] / 1024,
                    d["p99_bytes"] / 1024,
                    d["max_bytes"] / 1024,
                    d["nulls"],
                    d["empty_strings"],
                ]
                for d in unique.values()
            ],
        )
    )
    sections.extend(
        [
            "[All dataset measurements](dataset-profiles.csv)",
            "## Narrow query matrix",
            "Two million ID/timestamp/UUID rows, five warmups and 30 measured lookups per shape. Client p95 is nearest rank; plan timing is one representative EXPLAIN, not a percentile.",
        ]
    )
    sections.append(
        markdown(
            [
                "Engine",
                "Partitions",
                "Shape",
                "Client median ms",
                "Client p95 ms",
                "Plan ms",
                "Execution ms",
                "Plan nodes",
            ],
            [
                [
                    q["target"],
                    q["partitions"],
                    q["shape"],
                    q["median_ms"],
                    q["p95_ms"],
                    q["planning_ms"],
                    q["execution_ms"],
                    q["plan_nodes"],
                ]
                for q in queries
            ],
        )
    )
    sections.extend(
        [
            "[Query metrics and buffers](query-matrix.csv) · Complete JSON plans are inside each compressed receipt.",
            "## Full-row EF / BenchmarkDotNet",
            "One launch, three warmups, ten measured iterations, 100 operations per invocation. Default outlier treatment may retain fewer than ten iterations. Mean/stddev are per operation; these are not individual-request p95 measurements.",
        ]
    )
    sections.append(
        markdown(
            [
                "Engine",
                "Method",
                "Parameters",
                "Mean ms",
                "Stddev ms",
                "Retained iterations",
                "Allocated bytes/op",
            ],
            [
                [
                    b["target"],
                    b["method"],
                    b["parameters"],
                    b["mean_ms"],
                    b["stddev_ms"],
                    b["retained_iterations"],
                    b["allocated_bytes_per_operation"],
                ]
                for b in bdn
            ],
        )
    )
    sections.extend(
        [
            "[EF measurements](dotnet.csv)",
            "## Migration and scheduling",
            "Backfill changes both child tables, then verifies every original column. WAL is an isolated local cluster statement delta, excluding commit; Aurora WAL is unavailable.",
        ]
    )
    sections.append(
        markdown(
            [
                "Engine",
                "Trial",
                "Child rows",
                "Add columns s",
                "Backfill s",
                "Backfill WAL bytes",
                "Constraints s",
            ],
            [
                [
                    m["target"],
                    m["trial"],
                    m["child_rows"],
                    m["add_columns_server_seconds"],
                    m["backfill_server_seconds"],
                    m["backfill_statement_wal_bytes"],
                    m["constraints_server_seconds"],
                ]
                for m in migration
            ],
        )
    )
    sections.append(
        markdown(
            ["Engine", "Scheduling key", "Selected", "Moved", "Server s", "WAL bytes"],
            [
                [
                    s["target"],
                    s["design"],
                    s["selected_rows"],
                    s["rows_crossing_partition"],
                    s["server_seconds"],
                    s["wal_bytes"],
                ]
                for s in scheduling
            ],
        )
    )
    sections.extend(
        [
            "[Migration CSV](migration.csv) · [Scheduling CSV](scheduling.csv)",
            "## Archival boundaries",
        ]
    )
    sections.append(
        markdown(
            ["Engine", "Checks passed", "Full relations verified", "Bytes archived"],
            [
                [
                    f["target"],
                    f["checks_passed"],
                    f["relations_verified"],
                    f["archive_bytes"],
                ]
                for f in fidelity
            ],
        )
    )
    if cloud:
        sections.extend(
            [
                "## CloudWatch context",
                "Read-only four-hour context, not attribution to an individual arm. Mean is the mean of returned five-minute averages. Units are returned by CloudWatch; these are not an invoice or a local WAL equivalent.",
            ]
        )
        sections.append(
            markdown(
                [
                    "Metric",
                    "Points",
                    "Unit",
                    "Minimum",
                    "Mean of period averages",
                    "Maximum",
                ],
                [
                    [
                        c["metric"],
                        c["points"],
                        c["unit"],
                        c["minimum"],
                        c["mean_of_period_averages"],
                        c["maximum"],
                    ]
                    for c in cloud
                ],
            )
        )
        sections.append("[Full metric window and values](cloud-observation.csv)")
    (OUT / "performance-tables.md").write_text("\n\n".join(sections) + "\n")
    return aggregate


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--selection", type=Path)
    args = p.parse_args()
    entries = (
        json.loads(args.selection.read_text())
        if args.selection
        else json.loads((EVIDENCE / "manifest.json").read_text())["runs"]
    )
    build(entries)


if __name__ == "__main__":
    main()
