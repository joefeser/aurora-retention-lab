#!/usr/bin/env python3
"""Full-schema archival boundaries: 10 MiB body, Unicode, NULL/empty, quotes and corruption."""

import argparse
import json
import time
import uuid
import workload as w
from run_workload import Database, ROOT, archive, exact


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--target", choices=["postgres:16", "postgres:17", "aurora"], required=True
    )
    args = p.parse_args()
    dest = ROOT / ".lab" / ("fidelity-" + uuid.uuid4().hex[:12])
    dest.mkdir()
    record = {"passed": False, "target": args.target, "checks": []}
    db = None
    start = time.monotonic()
    try:
        run = uuid.uuid4().hex
        db = Database(args.target, "retention-fidelity-" + run[:10], record)
        s = "fidelity_" + run[:10]
        a = s + "_archive"
        record["engine"] = db.scalar("SELECT version()")
        db.batch(w.schema_sql(s))
        db.execute(w.seed_functions(s))
        db.execute(
            f"INSERT INTO {s}.notification_template(name) VALUES('Synthetic template café')"
        )
        db.execute(w.seed_parent(s, 1, 4, 4, "typical"))
        db.batch(w.seed_children(s, 4, "typical"))
        db.execute(
            f"UPDATE {s}.notification_event SET scheduled_at='2026-09-01',content_body=CASE id WHEN 1 THEN repeat('x',10485760) WHEN 2 THEN '<html>café – 🚀, \"quoted\"'||chr(10)||'new line</html>' WHEN 3 THEN NULL ELSE '' END"
        )
        db.execute(
            f"UPDATE {s}.delivery_queue SET content_body=CASE id WHEN 1 THEN NULL WHEN 2 THEN '' WHEN 3 THEN 'café, \"quoted\"'||chr(10)||'line' ELSE '<html>synthetic</html>' END,send_details=jsonb_build_object('email','test@example.invalid','nested',jsonb_build_object('unicode','🚀','nullable',NULL))"
        )
        db.begin()
        db.execute(
            "LOCK TABLE "
            + ",".join(f"{s}.{t}" for t in w.TABLES)
            + " IN ACCESS EXCLUSIVE MODE"
        )
        record["archives"] = archive(db, s, "delete", a, run, "full-schema-boundaries")
        db.commit()
        record["checks"].append("all five full-schema relations round-trip exactly")
        if (
            db.scalar(
                f"SELECT octet_length(content_body) FROM {a}.notification_event WHERE id=1"
            )
            != 10485760
        ):
            raise RuntimeError("Large body boundary lost")
        record["checks"].append("10 MiB ASCII body preserved at declared varchar limit")
        db.begin()
        db.execute(
            f"UPDATE {a}.notification_event SET content_body='corrupted' WHERE id=2"
        )
        try:
            exact(db, f"TABLE {s}.notification_event", f"TABLE {a}.notification_event")
        except RuntimeError:
            record["checks"].append("same-row-count content corruption rejected")
        else:
            raise RuntimeError("Corruption was accepted")
        db.rollback()
        exact(db, f"TABLE {s}.notification_event", f"TABLE {a}.notification_event")
        db.batch([f"DROP SCHEMA {s} CASCADE", f"DROP SCHEMA {a} CASCADE"])
        record["passed"] = True
    except Exception as exc:
        record["failure"] = {"type": type(exc).__name__, "message": str(exc)}
        raise
    finally:
        if db:
            db.close()
        record["elapsed_seconds"] = time.monotonic() - start
        (dest / "evidence.json").write_text(
            json.dumps(record, indent=2, default=str) + "\n"
        )
        print("Evidence:", dest / "evidence.json", flush=True)


if __name__ == "__main__":
    main()
