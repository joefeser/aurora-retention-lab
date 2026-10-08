"""Opt-in local PG17 recovery proof; creates and removes its own Docker database."""

import tempfile, uuid, json
from pathlib import Path
from run_workload import Database
from cloud_recovery import Inventory, recover


def main():
    record = {"passed": False}
    db = Database("postgres:17", "retention-recovery-" + uuid.uuid4().hex[:10], record)
    try:
        with tempfile.TemporaryDirectory() as directory:
            outputs = {
                "ClusterArn": "synthetic-local-only",
                "Bucket": "synthetic-local-only",
            }
            db.outputs = outputs
            db.inventory = Inventory(directory, outputs)
            db.begin()
            db.batch(
                [
                    "CREATE SCHEMA recovery_owned",
                    "CREATE TABLE recovery_owned.rows(id int)",
                    "INSERT INTO recovery_owned.rows VALUES(1)",
                    "ALTER SCHEMA recovery_owned RENAME TO recovery_owned_old",
                ]
            )
            db.commit()  # Simulate a later client failure after a durable commit.
            assert db.scalar("SELECT count(*) FROM recovery_owned_old.rows") == 1
            result = recover(db, db.inventory.path, execute=True)
            assert result["state"] == "recovered", result
            assert (
                db.scalar(
                    "SELECT count(*) FROM pg_namespace WHERE nspname LIKE 'recovery_owned%'"
                )
                == 0
            )
            # Collision with an existing, unmarked schema must never gain our marker.
            db.inventory = None
            db.execute("CREATE SCHEMA recovery_foreign")
            db.execute("CREATE TABLE recovery_foreign.rows(id int)")
            db.inventory = Inventory(directory, outputs)
            try:
                db.batch(["CREATE SCHEMA recovery_foreign"])
            except Exception:
                pass
            else:
                raise AssertionError("collision accepted")
            result = recover(db, db.inventory.path, execute=True)
            assert result["state"] == "recovery_incomplete", result
            assert (
                db.scalar(
                    "SELECT count(*) FROM pg_namespace WHERE nspname='recovery_foreign'"
                )
                == 1
            )
            # Container cleanup owns the whole local database; the recovery path must refuse it.
            db.inventory = None
        record["passed"] = True
    finally:
        db.close()
        Path(".lab/recovery-smoke-evidence.json").write_text(
            json.dumps(record, indent=2) + "\n"
        )
    print(json.dumps(record, indent=2))
    assert record["passed"] and record["container_removed"]


if __name__ == "__main__":
    main()
