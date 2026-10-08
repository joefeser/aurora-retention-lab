public static class Schema
{
    public const string HistoryIdentityGuard = """
        DO $$ BEGIN
          IF EXISTS (SELECT id FROM partitioned.delivery_queue GROUP BY id HAVING count(*) > 1) THEN
            RAISE EXCEPTION 'bare queue IDs are ambiguous for soft history; refuse archival' USING ERRCODE='23505';
          END IF;
        END $$;
        """;
    public const string CopyBaseline = """
        INSERT INTO partitioned.notification_template OVERRIDING SYSTEM VALUE SELECT * FROM baseline.notification_template;
        INSERT INTO partitioned.notification_event OVERRIDING SYSTEM VALUE SELECT * FROM baseline.notification_event;
        INSERT INTO partitioned.delivery_queue(id,notification_event_id,parent_scheduled_at,scheduled_at,send_state,template_id)
          OVERRIDING SYSTEM VALUE SELECT q.id,q.notification_event_id,p.scheduled_at,q.scheduled_at,q.send_state,q.template_id
          FROM baseline.delivery_queue q JOIN baseline.notification_event p ON p.id=q.notification_event_id;
        INSERT INTO partitioned.push_delivery(id,notification_event_id,parent_scheduled_at)
          OVERRIDING SYSTEM VALUE SELECT q.id,q.notification_event_id,p.scheduled_at
          FROM baseline.push_delivery q JOIN baseline.notification_event p ON p.id=q.notification_event_id;
        INSERT INTO partitioned.send_history OVERRIDING SYSTEM VALUE SELECT * FROM baseline.send_history;
        SELECT setval(pg_get_serial_sequence('partitioned.notification_template','id'),(SELECT max(id) FROM partitioned.notification_template));
        SELECT setval(pg_get_serial_sequence('partitioned.notification_event','id'),(SELECT max(id) FROM partitioned.notification_event));
        SELECT setval(pg_get_serial_sequence('partitioned.delivery_queue','id'),(SELECT max(id) FROM partitioned.delivery_queue));
        SELECT setval(pg_get_serial_sequence('partitioned.push_delivery','id'),(SELECT max(id) FROM partitioned.push_delivery));
        SELECT setval(pg_get_serial_sequence('partitioned.send_history','id'),(SELECT max(id) FROM partitioned.send_history));
        """;
    public static string Create(bool partitioned)
    {
        var s = partitioned ? "partitioned" : "baseline";
        var parentKey = partitioned ? ", scheduled_at" : "";
        var childKey = partitioned ? ", parent_scheduled_at" : "";
        var childColumn = partitioned ? "parent_scheduled_at timestamptz NOT NULL," : "";
        var parentPartition = partitioned ? "PARTITION BY RANGE(scheduled_at)" : "";
        var childPartition = partitioned ? "PARTITION BY RANGE(parent_scheduled_at)" : "";
        var ddl = $"""
            CREATE SCHEMA {s};
            CREATE TABLE {s}.notification_template(id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, name text NOT NULL);
            CREATE TABLE {s}.notification_event(id bigint GENERATED ALWAYS AS IDENTITY, scheduled_at timestamptz NOT NULL,
              content_body text NOT NULL, external_ref_id text NOT NULL, template_id bigint NOT NULL REFERENCES {s}.notification_template ON DELETE CASCADE,
              PRIMARY KEY(id{parentKey})) {parentPartition};
            CREATE TABLE {s}.delivery_queue(id bigint GENERATED ALWAYS AS IDENTITY, notification_event_id bigint NOT NULL,
              {childColumn} scheduled_at timestamptz NOT NULL, send_state text NOT NULL,
              template_id bigint NOT NULL REFERENCES {s}.notification_template ON DELETE CASCADE,
              PRIMARY KEY(id{childKey}), FOREIGN KEY(notification_event_id{childKey}) REFERENCES {s}.notification_event(id{parentKey}) ON DELETE CASCADE) {childPartition};
            CREATE TABLE {s}.push_delivery(id bigint GENERATED ALWAYS AS IDENTITY, notification_event_id bigint NOT NULL,
              {childColumn} PRIMARY KEY(id{childKey}), FOREIGN KEY(notification_event_id{childKey}) REFERENCES {s}.notification_event(id{parentKey}) ON DELETE CASCADE) {childPartition};
            CREATE TABLE {s}.send_history(id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, delivery_queue_id bigint NOT NULL, detail text NOT NULL);
            CREATE INDEX ON {s}.delivery_queue(notification_event_id{childKey});
            CREATE INDEX ON {s}.push_delivery(notification_event_id{childKey});
            CREATE INDEX ON {s}.send_history(delivery_queue_id);
            """;
        if (partitioned)
        {
            foreach (var table in new[] { "notification_event", "delivery_queue", "push_delivery" })
                ddl += $"""
                    CREATE TABLE {s}.{table}_old PARTITION OF {s}.{table} FOR VALUES FROM ('2026-09-01') TO ('2026-09-02');
                    CREATE TABLE {s}.{table}_live PARTITION OF {s}.{table} FOR VALUES FROM ('2026-10-01') TO ('2026-10-02');
                    """;
            ddl += """
                CREATE FUNCTION partitioned.reject_retention_change() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                  IF NEW.scheduled_at IS DISTINCT FROM OLD.scheduled_at THEN
                    RAISE EXCEPTION 'retention timestamp is immutable' USING ERRCODE='23514';
                  END IF;
                  RETURN NEW;
                END $$;
                CREATE TRIGGER retention_immutable BEFORE UPDATE ON partitioned.notification_event
                  FOR EACH ROW EXECUTE FUNCTION partitioned.reject_retention_change();
                CREATE FUNCTION partitioned.reject_child_retention_change() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                  IF NEW.parent_scheduled_at IS DISTINCT FROM OLD.parent_scheduled_at THEN
                    RAISE EXCEPTION 'child retention timestamp is immutable' USING ERRCODE='23514';
                  END IF;
                  RETURN NEW;
                END $$;
                CREATE TRIGGER retention_immutable BEFORE UPDATE ON partitioned.delivery_queue
                  FOR EACH ROW EXECUTE FUNCTION partitioned.reject_child_retention_change();
                CREATE TRIGGER retention_immutable BEFORE UPDATE ON partitioned.push_delivery
                  FOR EACH ROW EXECUTE FUNCTION partitioned.reject_child_retention_change();
                """;
        }
        return ddl;
    }
}
