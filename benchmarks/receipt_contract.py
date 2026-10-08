"""Version 1 public receipt contracts; stdlib only, shared by collection and publication."""

import datetime as dt
import json
import math
from pathlib import Path
import re

FIELDS = json.loads(Path(__file__).with_name("receipt-fields-v1.json").read_text())


def finite(value, *, minimum=None, integer=False):
    try:
        is_finite = type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        is_finite = False
    if (
        not is_finite
        or (integer and type(value) is not int)
        or (minimum is not None and value < minimum)
    ):
        raise ValueError(
            "Expected a finite numeric measurement with the declared bounds"
        )


def public_fields(kind, data, version=1):
    if type(version) is not int or version != 1 or kind not in FIELDS:
        raise ValueError("Unsupported receipt schema version or kind")
    if not isinstance(data, dict):
        raise ValueError("Receipt must be an object")
    allowed = FIELDS[kind]

    def visit(value, path):
        if isinstance(value, dict):
            if path not in allowed or not set(value) <= set(allowed[path]):
                raise ValueError("Unapproved public receipt fields")
            for key, child in value.items():
                if re.search(
                    r"token|password|secret|credential|private.?key|access.?key|account.?id|arn$",
                    key,
                    re.I,
                ):
                    raise ValueError("Credential or infrastructure field in receipt")
                visit(child, path + "." + key)
        elif isinstance(value, list):
            for child in value:
                visit(child, path + "[]")
        elif isinstance(value, float) and not math.isfinite(value):
            raise ValueError("Nonfinite receipt value")

    visit(data, "$")


def benchmark_case(case):
    if not isinstance(case, dict):
        raise ValueError("Invalid benchmark case")
    stats, memory = case.get("Statistics"), case.get("Memory")
    if not isinstance(stats, dict) or not isinstance(memory, dict):
        raise ValueError("Benchmark requires Statistics and Memory")
    finite(stats.get("N"), minimum=1, integer=True)
    for key in ("Mean", "StandardDeviation"):
        finite(stats.get(key), minimum=0)

    # BDN's remaining numeric statistics must not hide NaN, strings or booleans.
    def numeric_tree(value):
        if isinstance(value, dict):
            for item in value.values():
                numeric_tree(item)
        elif isinstance(value, list):
            for item in value:
                numeric_tree(item)
        else:
            finite(value)

    numeric_tree(stats)
    for key in (
        "Gen0Collections",
        "Gen1Collections",
        "Gen2Collections",
        "TotalOperations",
    ):
        finite(
            memory.get(key), minimum=1 if key == "TotalOperations" else 0, integer=True
        )
    finite(memory.get("BytesAllocatedPerOperation"), minimum=0)


def cloud_point(point):
    if not isinstance(point, dict):
        raise ValueError("Datapoint must be an object")
    stamp = point.get("Timestamp")
    if isinstance(stamp, str):
        try:
            stamp = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("Invalid datapoint timestamp") from None
    if not isinstance(stamp, dt.datetime) or stamp.utcoffset() is None:
        raise ValueError("Datapoint needs an aware timestamp")
    if not isinstance(point.get("Unit"), str) or not point["Unit"]:
        raise ValueError("Missing datapoint unit")
    for key in ("Minimum", "Average", "Maximum"):
        finite(point.get(key))
    if not point["Minimum"] <= point["Average"] <= point["Maximum"]:
        raise ValueError("Invalid datapoint range")
    return stamp


def cloud_metric(name, result):
    """Keep valid points but never classify a partially malformed metric as complete."""
    points, rejected = [], 0
    raw = result.get("Datapoints") if isinstance(result, dict) else None
    if not isinstance(raw, list):
        raw, rejected = [], 1
    for point in raw:
        try:
            stamp = cloud_point(point)
        except ValueError:
            rejected += 1
            continue
        points.append(
            {
                "Timestamp": stamp.isoformat(),
                **{k: point[k] for k in ("Unit", "Minimum", "Average", "Maximum")},
            }
        )
    points.sort(key=lambda p: dt.datetime.fromisoformat(p["Timestamp"]))
    return {
        "name": name,
        "points": points,
        "available": bool(points),
        "complete": rejected == 0,
        "rejected_points": rejected,
    }
