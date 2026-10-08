"""Durable inventory and bounded recovery for a stopped run in the existing lab."""

import hashlib
import json
from pathlib import Path
import re
import time
import uuid


def binding(outputs):
    return hashlib.sha256(
        (outputs["ClusterArn"] + "\n" + outputs["Bucket"]).encode()
    ).hexdigest()


class Inventory:
    def __init__(self, directory, outputs):
        self.token = uuid.uuid4().hex
        self.path = Path(directory) / ("recovery-" + self.token + ".json")
        self.data = {
            "schema_version": 1,
            "token": self.token,
            "lab_binding": binding(outputs),
            "schemas": [],
            "archive_prefix": None,
            "state": "retained",
        }

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.data, indent=2) + "\n")
        temp.replace(self.path)

    def prepare(self, statements):
        """Called on structured DDL statements, never on arbitrary SQL bodies."""
        result = []
        changed = False
        for statement in statements:
            create = re.fullmatch(r"CREATE SCHEMA ([a-z][a-z0-9_]{0,62})", statement)
            rename = re.fullmatch(
                r"ALTER SCHEMA ([a-z][a-z0-9_]{0,62}) RENAME TO ([a-z][a-z0-9_]{0,62})",
                statement,
            )
            result.append(statement)
            if create:
                name = create[1]
                if name not in self.data["schemas"]:
                    self.data["schemas"].append(name)
                # The CREATE and marker must execute atomically in the same SQL block.
                result.append(
                    f"COMMENT ON SCHEMA {name} IS 'retention-run:{self.token}'"
                )
                changed = True
            if rename:
                if rename[1] not in self.data["schemas"]:
                    raise ValueError(
                        "Cannot rename a schema outside this run inventory"
                    )
                if rename[2] not in self.data["schemas"]:
                    self.data["schemas"].append(rename[2])
                changed = True
        if changed:
            self.save()  # Write intent before sending DDL, including unknown RPC outcomes.
        return result

    def archive_prefix(self):
        self.data["archive_prefix"] = "runs/recovery-" + self.token + "/"
        self.save()  # S3 writes are not transactional with PostgreSQL.
        return self.data["archive_prefix"]


def recover(db, path, *, execute=False):
    path = Path(path)
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        raise ValueError("Invalid recovery inventory") from None
    if not isinstance(data, dict):
        raise ValueError("Invalid recovery inventory")
    token = data.get("token")
    if (
        type(data.get("schema_version")) is not int
        or data.get("schema_version") != 1
        or not isinstance(token, str)
        or not re.fullmatch(r"[a-f0-9]{32}", token)
        or data.get("lab_binding") != binding(db.outputs)
    ):
        raise ValueError("Recovery inventory is not bound to this lab")
    schemas = data.get("schemas")
    if (
        not isinstance(schemas, list)
        or len(schemas) > 1000
        or any(
            not isinstance(s, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", s)
            for s in schemas
        )
    ):
        raise ValueError("Invalid schema inventory")
    prefix = data.get("archive_prefix")
    if prefix not in (None, "runs/recovery-" + token + "/"):
        raise ValueError("Invalid archive inventory")
    if not execute:
        return {"schemas": schemas, "archive_prefix": prefix, "execute": False}
    data.pop("cleanup_error", None)
    data.pop("rollback_error", None)
    outcomes = []
    started = time.monotonic()

    def budget():
        if time.monotonic() - started > 120:
            raise TimeoutError("Recovery time budget reached; rerun after inspection")

    try:
        # Fail closed if versioning was enabled after provisioning. A delete marker
        # would not prove removal of the run's bytes.
        if prefix and db.s3.get_bucket_versioning(Bucket=db.outputs["Bucket"]).get(
            "Status"
        ):
            raise ValueError(
                "Versioned bucket requires explicit version-aware recovery"
            )
        for schema in schemas:
            budget()
            # Inspect ownership and DROP in one bounded transaction; absent is idempotent.
            db.begin()
            db.execute("SET LOCAL statement_timeout='8s'")
            db.execute("SET LOCAL lock_timeout='2s'")
            db.execute(f"""DO $recover$ BEGIN
              IF EXISTS (SELECT 1 FROM pg_namespace WHERE nspname='{schema}') THEN
                IF NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname='{schema}'
                  AND obj_description(oid,'pg_namespace')='retention-run:{token}') THEN
                  RAISE EXCEPTION 'Recovery ownership mismatch';
                END IF;
                EXECUTE 'DROP SCHEMA {schema} CASCADE';
              END IF; END $recover$""")
            db.commit()
            outcomes.append({"schema": schema, "removed_or_absent": True})
        if prefix:
            # Listing the exact random prefix also catches multi-file/unknown exports.
            # Ten deletions plus one read-only confirmation of the last batch.
            for deleted_batches in range(11):
                budget()
                page = db.s3.list_objects_v2(
                    Bucket=db.outputs["Bucket"], Prefix=prefix, MaxKeys=1000
                )
                objects = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
                if any(not obj["Key"].startswith(prefix) for obj in objects):
                    raise ValueError("Object outside owned prefix")
                if not objects:
                    if page.get("IsTruncated"):
                        raise RuntimeError("Incomplete archive listing")
                    break
                if deleted_batches == 10:
                    raise TimeoutError("Object recovery batch ceiling reached")
                result = db.s3.delete_objects(
                    Bucket=db.outputs["Bucket"],
                    Delete={"Objects": objects, "Quiet": True},
                )
                if result.get("Errors"):
                    raise RuntimeError("Some owned archive deletions failed")
        data["state"] = "recovered"
    except Exception as exc:
        data["state"] = "recovery_incomplete"
        data["cleanup_error"] = type(
            exc
        ).__name__  # No service message or credential values.
        try:
            db.rollback()
        except Exception as rollback_error:
            data["rollback_error"] = type(rollback_error).__name__
    data["outcomes"] = outcomes
    data["elapsed_seconds"] = time.monotonic() - started
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=2) + "\n")
    temp.replace(path)
    return {
        "state": data["state"],
        "cleanup_error": data.get("cleanup_error"),
        "schemas_processed": len(outcomes),
    }


def main():
    import argparse
    from run_workload import Database

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inventory", type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-run-stopped", action="store_true")
    args = parser.parse_args()
    if args.execute and not args.confirm_run_stopped:
        parser.error(
            "Stop the run and let any in-flight SQL/export finish, then pass --confirm-run-stopped"
        )
    record = {"passed": False}
    db = Database("aurora", "owned-run-recovery", record)
    try:
        result = recover(db, args.inventory, execute=args.execute)
        print(json.dumps(result, indent=2))
        if args.execute and result["state"] != "recovered":
            raise SystemExit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()
