#!/usr/bin/env python3
"""Measure source-shaped backfill and child scheduling; local retry collision proof."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import json
import time
import uuid
import psycopg
from psycopg.rows import dict_row
import workload as w
from run_workload import Database, ROOT, timed, exact


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--target", choices=["postgres:16", "postgres:17", "aurora"], required=True
    )
    p.add_argument("--rows", type=int, choices=[128, 4096, 16384], default=4096)
    args = p.parse_args()
    dest = ROOT / ".lab" / ("migration-" + uuid.uuid4().hex[:12])
    dest.mkdir()
    record = {
        "passed": False,
        "target": args.target,
        "rows": args.rows,
        "started_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "backfills": [],
    }
    db = None
    started = time.monotonic()
    try:
        db = Database(args.target, "retention-migrate-" + uuid.uuid4().hex[:10], record)
        root = "migration_" + uuid.uuid4().hex[:10]
        src = root + "_source"
        record["engine"] = db.scalar("SELECT version()")
        db.batch(
            [
                f"CREATE SCHEMA {root}",
                f"CREATE TABLE {root}.measurements(id bigint GENERATED ALWAYS AS IDENTITY,label text,seconds double precision,wal_bytes bigint)",
            ]
            + w.schema_sql(src)
        )
        db.execute(w.seed_functions(src))
        db.execute(
            f"INSERT INTO {src}.notification_template(name) VALUES('Synthetic template')"
        )
        for lo in range(1, args.rows + 1, 128):
            db.execute(
                w.seed_parent(src, lo, min(lo + 127, args.rows), args.rows, "tail")
            )
        db.batch(w.seed_children(src, args.rows, "skew"))
        db.batch([f"ANALYZE {src}.{t}" for t in w.TABLES])
        record["initial_counts"] = {
            t: db.scalar(f"SELECT count(*) FROM {src}.{t}") for t in w.TABLES
        }
        for trial in range(3):
            s = root + f"_b{trial}"
            db.batch(w.schema_sql(s))
            db.batch(w.copy_sql(src, s, "delete"))
            result = {"trial": trial + 1, "batches": []}
            db.begin()
            result["add_columns"] = timed(
                db,
                root,
                "add-" + s,
                [
                    f"ALTER TABLE {s}.{t} ADD COLUMN parent_scheduled_at timestamptz"
                    for t in ("delivery_queue", "push_delivery")
                ],
            )
            db.commit()
            for lo in range(1, args.rows + 1, 256):
                hi = min(lo + 255, args.rows)
                db.begin()
                measure = timed(
                    db,
                    root,
                    f"backfill-{s}-{lo}",
                    [
                        f"UPDATE {s}.{t} q SET parent_scheduled_at=p.scheduled_at FROM {s}.notification_event p WHERE q.notification_event_id=p.id AND p.id BETWEEN {lo} AND {hi}"
                        for t in ("delivery_queue", "push_delivery")
                    ],
                )
                db.commit()
                result["batches"].append(measure)
            db.begin()
            result["constraints"] = timed(
                db,
                root,
                "constraints-" + s,
                [f"CREATE UNIQUE INDEX ON {s}.notification_event(id,scheduled_at)"]
                + [
                    statement
                    for t in ("delivery_queue", "push_delivery")
                    for statement in (
                        f"ALTER TABLE {s}.{t} ALTER COLUMN parent_scheduled_at SET NOT NULL",
                        f"ALTER TABLE {s}.{t} ADD CONSTRAINT migrated_parent_fk FOREIGN KEY(notification_event_id,parent_scheduled_at) REFERENCES {s}.notification_event(id,scheduled_at) ON DELETE CASCADE NOT VALID",
                        f"ALTER TABLE {s}.{t} VALIDATE CONSTRAINT migrated_parent_fk",
                    )
                ],
            )
            db.commit()
            for t in ("delivery_queue", "push_delivery"):
                if (
                    db.scalar(
                        f"SELECT count(*) FROM {s}.{t} q JOIN {s}.notification_event p ON p.id=q.notification_event_id WHERE q.parent_scheduled_at IS DISTINCT FROM p.scheduled_at"
                    )
                    != 0
                ):
                    raise RuntimeError("Backfill mismatch")
                exact(
                    db,
                    w.logical_select(s, t, "delete"),
                    w.logical_select(src, t, "delete"),
                )
            result["child_rows"] = (
                record["initial_counts"]["delivery_queue"]
                + record["initial_counts"]["push_delivery"]
            )
            result["passed"] = True
            record["backfills"].append(result)
            db.execute(f"DROP SCHEMA {s} CASCADE")
        # Isolate physical row movement: same queue columns, partitioned on mutable delivery time.
        wrong = root + ".mutable_queue"
        db.execute(
            f"CREATE TABLE {wrong}("
            + ",".join(n + " " + t for n, t in w.QUEUE)
            + ",PRIMARY KEY(id,scheduled_at)) PARTITION BY RANGE(scheduled_at)"
        )
        db.batch(
            [
                f"CREATE TABLE {root}.mutable_d{day} PARTITION OF {wrong} FOR VALUES FROM ('2026-08-17'::date+{day}) TO ('2026-08-17'::date+{day+1})"
                for day in range(46)
            ]
            + [
                f"CREATE TABLE {root}.mutable_live PARTITION OF {wrong} FOR VALUES FROM ('2026-10-02') TO ('2026-12-01')"
            ]
        )
        db.execute(
            f"INSERT INTO {wrong} OVERRIDING SYSTEM VALUE SELECT * FROM {src}.delivery_queue"
        )
        right = root + "_aligned"
        db.batch(w.schema_sql(right, "partition"))
        db.batch(w.copy_sql(src, right, "partition"))
        record["scheduling"] = []
        for table, label in [
            (wrong, "mutable_delivery_key"),
            (right + ".delivery_queue", "immutable_parent_key"),
        ]:
            db.execute(
                f"CREATE TABLE {root}.before_move AS SELECT id,tableoid::oid AS relation_id FROM {table} WHERE id%10=1"
            )
            selected = db.scalar(f"SELECT count(*) FROM {root}.before_move")
            db.begin()
            measure = timed(
                db,
                root,
                "schedule-" + label,
                [
                    f"UPDATE {table} SET scheduled_at=scheduled_at+interval '12 hours' WHERE id%10=1"
                ],
            )
            db.commit()
            moved = db.scalar(
                f"SELECT count(*) FROM {table} q JOIN {root}.before_move b ON b.id=q.id WHERE q.tableoid::oid<>b.relation_id"
            )
            if label == "immutable_parent_key" and moved:
                raise RuntimeError("Parent-aligned queue moved")
            record["scheduling"].append(
                {
                    "design": label,
                    "selected_rows": selected,
                    "rows_crossing_partition": moved,
                    **measure,
                }
            )
            db.execute(f"DROP TABLE {root}.before_move")
        if args.target != "aurora":
            # READ COMMITTED updater waits on a moving tuple, receives 40001, then retries from a new transaction.
            peer = psycopg.connect(
                db.connection.info.dsn,
                password=db.connection.pgconn.password.decode(),
                autocommit=True,
                row_factory=dict_row,
            )
            peer.execute("SET statement_timeout='8s'")
            peer.execute("BEGIN")
            db.begin()
            db.execute(
                f"UPDATE {wrong} SET scheduled_at=scheduled_at+interval '1 day' WHERE id=2"
            )

            def contender():
                try:
                    peer.execute(f"UPDATE {wrong} SET send_state=3 WHERE id=2")
                    return "unexpected_success"
                except psycopg.Error as exc:
                    return exc.sqlstate

            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(contender)
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if (
                        db.scalar(
                            f"SELECT wait_event_type FROM pg_stat_activity WHERE pid={peer.info.backend_pid}"
                        )
                        == "Lock"
                    ):
                        break
                    time.sleep(0.02)
                else:
                    raise RuntimeError("Contender did not block on moving tuple")
                db.commit()
                state = future.result(timeout=10)
            peer.execute("ROLLBACK")
            if state != "40001":
                raise RuntimeError(
                    "Expected serialization failure on concurrent partition move: "
                    + str(state)
                )
            peer.execute("BEGIN")
            peer.execute(f"UPDATE {wrong} SET send_state=3 WHERE id=2")
            peer.execute("COMMIT")
            if db.scalar(f"SELECT send_state FROM {wrong} WHERE id=2") != 3:
                raise RuntimeError("Retry did not persist")
            peer.close()
            record["concurrent_move"] = {
                "sqlstate": state,
                "retry_new_transaction_passed": True,
                "isolation": "READ COMMITTED",
            }
        else:
            record["concurrent_move"] = {
                "tested": False,
                "reason": "Local wire-protocol collision fixture; no Aurora TCP/proxy claim",
            }
        db.batch(
            [
                f"DROP SCHEMA {right} CASCADE",
                f"DROP SCHEMA {src} CASCADE",
                f"DROP SCHEMA {root} CASCADE",
            ]
        )
        record["passed"] = True
    except Exception as exc:
        record["failure"] = {"type": type(exc).__name__, "message": str(exc)}
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
