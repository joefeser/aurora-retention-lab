"""Canonical synthetic workload derived from the supplied sanitized DDL and review.

Unknown push/history/template payload columns are intentionally minimal and labeled
in the report. Names are generic; parent/queue types and nullability are preserved.
"""

import re

PARENT = [
    ("id", "bigint GENERATED ALWAYS AS IDENTITY"),
    ("recipient_id", "varchar(10) NOT NULL"),
    ("message_text", "varchar(1000)"),
    ("subject_text", "varchar(100) NOT NULL"),
    ("scheduled_at", "timestamptz NOT NULL"),
    ("expires_at", "timestamptz"),
    ("notification_template_id", "integer NOT NULL"),
    ("created_at", "timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP"),
    ("external_ref_id", "uuid NOT NULL DEFAULT gen_random_uuid()"),
    ("send_state", "smallint NOT NULL DEFAULT 0"),
    ("content_body", "varchar(10485760)"),
    ("push_notification_text", "text"),
    ("sms_notification_text", "text"),
    ("email_notification_text", "text"),
    ("target_time", "timestamptz"),
    ("metadata", "jsonb"),
    ("remaining_minutes", "integer NOT NULL DEFAULT 0"),
]
QUEUE = [
    ("id", "bigint GENERATED ALWAYS AS IDENTITY"),
    ("recipient_id", "varchar(10) NOT NULL"),
    ("notification_template_id", "integer NOT NULL"),
    ("notification_event_id", "bigint NOT NULL"),
    ("message_text", "text"),
    ("subject_text", "varchar(100) NOT NULL"),
    ("send_details", "jsonb NOT NULL"),
    ("all_targets_attempted", "boolean NOT NULL"),
    ("expires_at", "timestamptz"),
    ("scheduled_at", "timestamptz NOT NULL"),
    ("created_at", "timestamptz NOT NULL"),
    ("send_state", "smallint NOT NULL"),
    ("content_body", "varchar(10485760)"),
    ("push_notification_text", "text"),
    ("sms_notification_text", "text"),
    ("email_notification_text", "text"),
    ("metadata", "jsonb"),
]
PAYLOAD = {
    "message_text",
    "subject_text",
    "content_body",
    "push_notification_text",
    "sms_notification_text",
    "email_notification_text",
    "metadata",
}
TABLES = (
    "notification_event",
    "delivery_queue",
    "push_delivery",
    "send_history",
    "notification_template",
)
ANCHOR = "'2026-10-01 00:00Z'::timestamptz"
PROFILES = {
    "typical": {
        "parent_sizes": [5120, 10240, 20480, 30720, 51200],
        "queue_sizes": [3072, 4096, 6144, 8192],
        "tail": False,
        "skew": False,
    },
    "tail": {
        "parent_sizes": [5120, 10240, 20480, 30720, 51200],
        "queue_sizes": [3072, 4096, 6144, 8192],
        "tail": True,
        "skew": False,
    },
    "skew": {
        "parent_sizes": [5120, 10240, 20480, 30720, 51200],
        "queue_sizes": [3072, 4096, 6144, 8192],
        "tail": True,
        "skew": True,
    },
}


def identifier(value):
    if not re.fullmatch("[a-z][a-z0-9_]{0,62}", value):
        raise ValueError("Unsafe generated identifier")
    return value


def columns(table):
    return [x[0] for x in (PARENT if table == "notification_event" else QUEUE)]


def schema_sql(schema, mode="delete", indexed=True, days=45):
    s = identifier(schema)
    if mode not in ("delete", "partition", "split", "split_daily", "copy"):
        raise ValueError(mode)
    partition = mode == "partition"
    parent_columns = [
        (n, t) for n, t in PARENT if not mode.startswith("split") or n not in PAYLOAD
    ]
    pk = "id,scheduled_at" if partition else "id"
    childkey = "id,parent_scheduled_at" if partition else "id"
    fk = (
        "notification_event_id,parent_scheduled_at"
        if partition
        else "notification_event_id"
    )
    statements = [
        f"CREATE SCHEMA {s}",
        f"CREATE TABLE {s}.notification_template(id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,name text NOT NULL)",
    ]
    statements.append(
        f"CREATE TABLE {s}.notification_event("
        + ",".join(n + " " + t for n, t in parent_columns)
        + f",PRIMARY KEY({pk}),FOREIGN KEY(notification_template_id) REFERENCES {s}.notification_template(id) ON DELETE CASCADE)"
        + (" PARTITION BY RANGE(scheduled_at)" if partition else "")
    )
    qcolumns = QUEUE + (
        [("parent_scheduled_at", "timestamptz NOT NULL")] if partition else []
    )
    statements.append(
        f"CREATE TABLE {s}.delivery_queue("
        + ",".join(n + " " + t for n, t in qcolumns)
        + f",PRIMARY KEY({childkey}),FOREIGN KEY({fk}) REFERENCES {s}.notification_event({pk}) ON DELETE CASCADE,FOREIGN KEY(notification_template_id) REFERENCES {s}.notification_template(id) ON DELETE CASCADE)"
        + (" PARTITION BY RANGE(parent_scheduled_at)" if partition else "")
    )
    statements.append(
        f"CREATE TABLE {s}.push_delivery(id bigint GENERATED ALWAYS AS IDENTITY,notification_event_id bigint NOT NULL,body varchar(200) NOT NULL"
        + (",parent_scheduled_at timestamptz NOT NULL" if partition else "")
        + f",PRIMARY KEY({childkey}),FOREIGN KEY({fk}) REFERENCES {s}.notification_event({pk}) ON DELETE CASCADE)"
        + (" PARTITION BY RANGE(parent_scheduled_at)" if partition else "")
    )
    statements.append(
        f"CREATE TABLE {s}.send_history(id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,delivery_queue_id bigint NOT NULL,detail text NOT NULL)"
    )
    statements.append(f"CREATE INDEX ON {s}.send_history(delivery_queue_id)")
    if indexed:
        for table in ("delivery_queue", "push_delivery"):
            statements.append(f"CREATE INDEX ON {s}.{table}({fk})")
    if partition:
        for table, key in (
            ("notification_event", "scheduled_at"),
            ("delivery_queue", "parent_scheduled_at"),
            ("push_delivery", "parent_scheduled_at"),
        ):
            for day in range(days):
                statements.append(
                    f"CREATE TABLE {s}.{table}_d{day} PARTITION OF {s}.{table} FOR VALUES FROM ('2026-08-17'::date+{day}) TO ('2026-08-17'::date+{day+1})"
                )
            statements.append(
                f"CREATE TABLE {s}.{table}_live PARTITION OF {s}.{table} FOR VALUES FROM ('2026-10-01') TO ('2026-12-01')"
            )
    if mode.startswith("split"):
        statements.append(
            f"CREATE TABLE {s}.event_payload(id bigint NOT NULL,scheduled_at timestamptz NOT NULL,"
            + ",".join(n + " " + t for n, t in PARENT if n in PAYLOAD)
            + f",PRIMARY KEY(id,scheduled_at),FOREIGN KEY(id) REFERENCES {s}.notification_event(id) ON DELETE CASCADE) PARTITION BY RANGE(scheduled_at)"
        )
        if mode == "split_daily":
            for day in range(days):
                statements.append(
                    f"CREATE TABLE {s}.event_payload_d{day} PARTITION OF {s}.event_payload FOR VALUES FROM ('2026-08-17'::date+{day}) TO ('2026-08-17'::date+{day+1})"
                )
        else:
            statements.append(
                f"CREATE TABLE {s}.event_payload_old PARTITION OF {s}.event_payload FOR VALUES FROM ('2026-08-17') TO ('2026-10-01')"
            )
        statements.append(
            f"CREATE TABLE {s}.event_payload_live PARTITION OF {s}.event_payload FOR VALUES FROM ('2026-10-01') TO ('2026-12-01')"
        )
    return statements


def logical_select(schema, table, mode):
    s = identifier(schema)
    if table == "notification_event" and mode.startswith("split"):
        return (
            "SELECT "
            + ",".join(("b." if n in PAYLOAD else "a.") + n for n, _ in PARENT)
            + f" FROM {s}.notification_event a JOIN {s}.event_payload b ON b.id=a.id AND b.scheduled_at=a.scheduled_at"
        )
    if table in ("notification_event", "delivery_queue"):
        return "SELECT " + ",".join(columns(table)) + f" FROM {s}.{table}"
    if table == "push_delivery":
        return f"SELECT id,notification_event_id,body FROM {s}.{table}"
    return f"SELECT * FROM {s}.{table}"


def copy_sql(source, dest, mode, where="true"):
    src, dst = identifier(source), identifier(dest)
    result = [
        f"INSERT INTO {dst}.notification_template OVERRIDING SYSTEM VALUE SELECT * FROM {src}.notification_template"
    ]
    parentcols = [
        n for n, _ in PARENT if not mode.startswith("split") or n not in PAYLOAD
    ]
    result.append(
        f"INSERT INTO {dst}.notification_event("
        + ",".join(parentcols)
        + ") OVERRIDING SYSTEM VALUE SELECT "
        + ",".join(parentcols)
        + f" FROM {src}.notification_event WHERE {where}"
    )
    if mode.startswith("split"):
        pc = ["id", "scheduled_at"] + [n for n, _ in PARENT if n in PAYLOAD]
        result.append(
            f"INSERT INTO {dst}.event_payload("
            + ",".join(pc)
            + ") SELECT "
            + ",".join(pc)
            + f" FROM {src}.notification_event WHERE {where}"
        )
    for table in ("delivery_queue", "push_delivery"):
        cols = (
            columns(table)
            if table == "delivery_queue"
            else ["id", "notification_event_id", "body"]
        )
        target = cols + (["parent_scheduled_at"] if mode == "partition" else [])
        projection = ["q." + n for n in cols] + (
            ["p.scheduled_at"] if mode == "partition" else []
        )
        result.append(
            f"INSERT INTO {dst}.{table}("
            + ",".join(target)
            + ") OVERRIDING SYSTEM VALUE SELECT "
            + ",".join(projection)
            + f" FROM {src}.{table} q JOIN {src}.notification_event p ON p.id=q.notification_event_id WHERE "
            + where.replace("scheduled_at", "p.scheduled_at")
        )
    result.append(
        f"INSERT INTO {dst}.send_history OVERRIDING SYSTEM VALUE SELECT h.* FROM {src}.send_history h JOIN {src}.delivery_queue q ON q.id=h.delivery_queue_id JOIN {src}.notification_event p ON p.id=q.notification_event_id WHERE "
        + where.replace("scheduled_at", "p.scheduled_at")
    )
    return result


def seed_functions(schema):
    s = identifier(schema)
    return f"""CREATE FUNCTION {s}.html(seed bigint,bytes integer) RETURNS text LANGUAGE sql IMMUTABLE AS $f$
      SELECT left('<html><style>td {{padding:8px;color:#243746}}</style><body><table>'||
        string_agg('<tr><td>Schedule update</td><td>'||md5(seed::text||':'||b::text)||'</td><td>Please review your next assignment.</td></tr>','' ORDER BY b)||
        '</table></body></html>',bytes)
      FROM generate_series(1,bytes/100+2) b $f$"""


def seed_parent(schema, lo, hi, rows, profile):
    s = identifier(schema)
    p = PROFILES[profile]
    size = "(ARRAY[5120,10240,20480,30720,51200])[1+(n%5)]"
    if p["tail"]:
        size = f"CASE WHEN n%100=0 THEN 1048576 WHEN n%20=0 THEN 204800 ELSE {size} END"
    # Source gives ranges, not percentiles/frequencies: these deterministic mixes are declared assumptions.
    return f"""INSERT INTO {s}.notification_event OVERRIDING SYSTEM VALUE
      SELECT n,'R'||lpad((n%100000)::text,9,'0'),
       CASE WHEN n%23=0 THEN NULL WHEN n%29=0 THEN '' ELSE 'Your schedule has been updated. Please review your assignments for the upcoming duty period.' END,
       'Schedule update '||n,
       CASE WHEN n<={rows*4//5} THEN '2026-08-17 12:00Z'::timestamptz+(n%45)*interval '1 day'
            ELSE '2026-10-01 12:00Z'::timestamptz+(n%30)*interval '1 day' END,
       CASE WHEN n%7=0 THEN NULL ELSE '2026-12-01'::timestamptz END,
       1,'2026-08-01'::timestamptz+(n%40)*interval '1 day',md5('synthetic-event-'||n)::uuid,(n%4)::smallint,
       CASE WHEN n%23=0 THEN NULL WHEN n%29=0 THEN '' ELSE {s}.html(n,{size}) END,
       left(repeat('Synthetic mobile notification. ',8),150),left(repeat('Synthetic SMS update. ',8),160),
       repeat('Plain text email fallback with \\"quotes\\" and a new line.'||chr(10),20),
       CASE WHEN n%3=0 THEN NULL ELSE '2026-11-01'::timestamptz END,
       CASE WHEN n%11=0 THEN NULL ELSE jsonb_build_object('base','AAA','duty_code','P1','effective_date','2026-10-15','synthetic_id',n) END,
       (n%120)::integer FROM generate_series({lo},{hi}) n"""


def seed_children(schema, rows, profile):
    s = identifier(schema)
    fanout = (
        "CASE WHEN p.id%20=0 THEN 40 ELSE 2 END" if PROFILES[profile]["skew"] else "3"
    )
    return [
        f"INSERT INTO {s}.delivery_queue OVERRIDING SYSTEM VALUE SELECT id,recipient_id,notification_template_id,id,message_text,subject_text,jsonb_build_object('channels',jsonb_build_array('push','email'),'email','user@example.invalid','device_tokens',jsonb_build_array('synthetic-'||id)),false,expires_at,scheduled_at+CASE WHEN id%10=0 THEN interval '12 hours' ELSE interval '0' END,created_at,send_state,CASE WHEN content_body IS NULL THEN NULL WHEN content_body='' THEN '' ELSE {s}.html(id,(ARRAY[3072,4096,6144,8192])[1+(id%4)]) END,push_notification_text,sms_notification_text,email_notification_text,metadata FROM {s}.notification_event",
        f"INSERT INTO {s}.push_delivery OVERRIDING SYSTEM VALUE SELECT p.id*100+b,p.id,left(repeat('Synthetic push notification '||p.id||' ',12),150) FROM {s}.notification_event p CROSS JOIN LATERAL generate_series(1,{fanout}) b",
        f"INSERT INTO {s}.send_history OVERRIDING SYSTEM VALUE SELECT id,id,'Synthetic delivery history with newline'||chr(10)||'and \"quotes\"' FROM {s}.delivery_queue WHERE id%4=0",
    ]
