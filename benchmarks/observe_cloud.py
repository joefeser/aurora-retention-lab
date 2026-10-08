#!/usr/bin/env python3
"""Read-only CloudWatch context for the existing lab; no account/endpoint identifiers in output."""

import datetime as dt
import json
import time
import uuid
import boto3
from run_workload import Database, ROOT


def main():
    dest = ROOT / ".lab" / ("cloud-observation-" + uuid.uuid4().hex[:12])
    dest.mkdir()
    record = {
        "passed": False,
        "target": "aurora",
        "scope": "Context over a shared window, not attribution to an individual SQL statement",
        "metrics": [],
    }
    db = None
    started = time.monotonic()
    try:
        db = Database("aurora", "read-only-cloud-observation", record)
        cluster = db.rds.describe_db_clusters(
            DBClusterIdentifier=db.outputs["ClusterArn"]
        )["DBClusters"][0]
        instance = cluster["DBClusterMembers"][0]["DBInstanceIdentifier"]
        cw = boto3.client("cloudwatch", region_name=db.region)
        end = dt.datetime.now(dt.timezone.utc)
        start = end - dt.timedelta(hours=4)
        record.update(
            window_start_utc=start.isoformat(),
            window_end_utc=end.isoformat(),
            period_seconds=300,
        )
        for metric, dimension, value in [
            ("CPUUtilization", "DBInstanceIdentifier", instance),
            ("ServerlessDatabaseCapacity", "DBInstanceIdentifier", instance),
            ("ACUUtilization", "DBInstanceIdentifier", instance),
            ("FreeableMemory", "DBInstanceIdentifier", instance),
            ("BufferCacheHitRatio", "DBInstanceIdentifier", instance),
            ("VolumeWriteIOPs", "DBClusterIdentifier", cluster["DBClusterIdentifier"]),
            ("VolumeReadIOPs", "DBClusterIdentifier", cluster["DBClusterIdentifier"]),
        ]:
            result = cw.get_metric_statistics(
                Namespace="AWS/RDS",
                MetricName=metric,
                Dimensions=[{"Name": dimension, "Value": value}],
                StartTime=start,
                EndTime=end,
                Period=300,
                Statistics=["Average", "Minimum", "Maximum"],
            )
            points = sorted(result["Datapoints"], key=lambda p: p["Timestamp"])
            record["metrics"].append(
                {"name": metric, "points": points, "available": bool(points)}
            )
        record["engine"] = db.scalar("SELECT version()")
        record["passed"] = True
    finally:
        if db:
            db.close()
        record["elapsed_seconds"] = time.monotonic() - started
        (dest / "evidence.json").write_text(
            json.dumps(record, indent=2, default=str) + "\n"
        )
        print("Evidence:", dest / "evidence.json")


if __name__ == "__main__":
    main()
