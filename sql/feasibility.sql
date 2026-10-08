-- Synthetic, destructive only inside an outer transaction in a disposable database.
-- The runner creates an isolated container; do not run this against production.
BEGIN;
SET TIME ZONE 'UTC';
SET statement_timeout = '30s';
SET lock_timeout = '2s';
CREATE SCHEMA retention_lab;
SET search_path = retention_lab, pg_catalog;
CREATE TABLE evidence (test text PRIMARY KEY, detail text NOT NULL);

CREATE FUNCTION assert_true(ok boolean, label text, detail text) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
  IF ok IS DISTINCT FROM true THEN RAISE EXCEPTION 'FAIL: %', label; END IF;
  INSERT INTO evidence VALUES (label, detail);
END $$;

CREATE FUNCTION expect_error(command text, expected_state text, label text) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE actual_state text; message text;
BEGIN
  BEGIN
    EXECUTE command;
  EXCEPTION WHEN OTHERS THEN
    GET STACKED DIAGNOSTICS actual_state = RETURNED_SQLSTATE, message = MESSAGE_TEXT;
  END;
  IF actual_state IS DISTINCT FROM expected_state THEN
    RAISE EXCEPTION 'FAIL: %, expected %, got %', label, expected_state, actual_state;
  END IF;
  INSERT INTO evidence VALUES (label, actual_state || ': ' || message);
END $$;

SELECT version() AS engine;
SELECT expect_error(
  'CREATE TABLE bad_key (id bigint PRIMARY KEY, scheduled_at timestamptz NOT NULL) PARTITION BY RANGE(scheduled_at)',
  '0A000', '01 ID-only primary key rejected');

CREATE TABLE event (
  id bigint GENERATED ALWAYS AS IDENTITY,
  scheduled_at timestamptz NOT NULL,
  body text,
  PRIMARY KEY (id, scheduled_at)
) PARTITION BY RANGE (scheduled_at);
CREATE TABLE event_old PARTITION OF event FOR VALUES FROM ('2026-09-01') TO ('2026-09-02');
CREATE TABLE event_live PARTITION OF event FOR VALUES FROM ('2026-10-01') TO ('2026-10-02');
INSERT INTO event(scheduled_at, body) VALUES ('2026-09-01 12:00Z', E'old\n"quoted",payload'),
  ('2026-10-01 12:00Z', 'live');
SELECT assert_true((SELECT count(DISTINCT id)=2 FROM event),
  '02 Partitioned parent identity works', 'Generated identity values route through a partitioned parent');
SELECT expect_error(
  $$INSERT INTO event(id, scheduled_at) VALUES (99, '2026-10-01 12:00Z')$$,
  '428C9', '03 GENERATED ALWAYS rejects ordinary explicit IDs');
SELECT expect_error(
  'CREATE TABLE bad_fk (event_id bigint REFERENCES event(id))',
  '42830', '04 ID-only foreign key rejected');

CREATE TABLE queue (
  id bigint NOT NULL,
  event_id bigint NOT NULL,
  parent_scheduled_at timestamptz NOT NULL,
  scheduled_at timestamptz NOT NULL,
  PRIMARY KEY (id, parent_scheduled_at),
  UNIQUE (event_id, parent_scheduled_at),
  FOREIGN KEY (event_id, parent_scheduled_at) REFERENCES event(id, scheduled_at) ON DELETE CASCADE
) PARTITION BY RANGE (parent_scheduled_at);
CREATE TABLE queue_old PARTITION OF queue FOR VALUES FROM ('2026-09-01') TO ('2026-09-02');
CREATE TABLE queue_live PARTITION OF queue FOR VALUES FROM ('2026-10-01') TO ('2026-10-02');
CREATE TABLE push_delivery (
  id bigint PRIMARY KEY,
  event_id bigint NOT NULL,
  parent_scheduled_at timestamptz NOT NULL,
  FOREIGN KEY (event_id, parent_scheduled_at) REFERENCES event(id, scheduled_at) ON DELETE CASCADE
);
INSERT INTO queue SELECT id, id, scheduled_at, scheduled_at FROM event;
INSERT INTO push_delivery SELECT id, id, scheduled_at FROM event;
SELECT expect_error(
  $$INSERT INTO queue VALUES (999, 1, '2026-09-01 12:00Z', '2026-09-01 12:00Z')$$,
  '23505', '05 One-to-one enforced per composite parent');
SELECT expect_error(
  $$INSERT INTO queue VALUES (999, 999, '2026-09-01 12:00Z', '2026-09-01 12:00Z')$$,
  '23503', '06 Orphan insertion rejected');
UPDATE queue SET scheduled_at = '2026-10-01 15:00Z' WHERE id = 1;
SELECT assert_true((SELECT tableoid = 'queue_old'::regclass FROM queue WHERE id=1),
  '07 Quiet-time update stays in parent-aligned partition',
  'Delivery scheduling can diverge without changing parent identity or child partition');

-- Explicit override demonstrates why a shared sequence is not UNIQUE(id).
INSERT INTO event(id, scheduled_at, body) OVERRIDING SYSTEM VALUE
VALUES (1, '2026-10-01 15:00Z', 'same bare ID, different identity pair');
SELECT assert_true((SELECT count(*)=2 FROM event WHERE id=1),
  '08 Composite key permits repeated bare ID', 'ID-only lookup is ambiguous after an explicit override');
DELETE FROM event WHERE id=1 AND scheduled_at='2026-10-01 15:00Z';

SELECT expect_error('ALTER TABLE event DETACH PARTITION event_old',
  '23503', '09 Referenced parent partition cannot detach');
SELECT expect_error('DROP TABLE event_old',
  '2BP01', '10 DROP RESTRICT preserves FK dependencies');

-- DDL CASCADE removes constraints, not child rows. Roll back the demonstration.
SAVEPOINT unsafe_drop;
DROP TABLE event_old CASCADE;
SELECT assert_true((SELECT count(*)=1 FROM queue WHERE id=1) AND
                  (SELECT count(*)=1 FROM push_delivery WHERE id=1),
  'temporary', 'DDL cascade kept referencing rows');
ROLLBACK TO SAVEPOINT unsafe_drop;
INSERT INTO evidence VALUES ('11 DROP CASCADE leaves referencing rows',
  'Observed inside a rolled-back savepoint; never use this as a retirement algorithm');

-- Ordinary row deletion really does cascade.
SAVEPOINT row_delete;
DELETE FROM event WHERE id=1 AND scheduled_at='2026-09-01 12:00Z';
SELECT assert_true(NOT EXISTS(SELECT FROM queue WHERE id=1) AND
                  NOT EXISTS(SELECT FROM push_delivery WHERE id=1),
  'temporary', 'Row deletion cascaded to both children');
ROLLBACK TO SAVEPOINT row_delete;
INSERT INTO evidence VALUES ('12 Row DELETE cascades', 'Both queue and push rows deleted inside rolled-back savepoint');

-- COPY fidelity is local only. The AWS stage must independently verify S3 exports.
CREATE TEMP TABLE roundtrip AS SELECT * FROM event WITH NO DATA;
COPY (SELECT * FROM event ORDER BY id, scheduled_at) TO '/tmp/retention-roundtrip.csv' WITH (FORMAT csv);
COPY roundtrip FROM '/tmp/retention-roundtrip.csv' WITH (FORMAT csv);
SELECT assert_true(NOT EXISTS(
  (SELECT * FROM event EXCEPT ALL SELECT * FROM roundtrip)
  UNION ALL (SELECT * FROM roundtrip EXCEPT ALL SELECT * FROM event)),
  '13 Local CSV round-trip exact comparison', 'EXCEPT ALL in both directions detects content and multiplicity differences');

-- Copy-keepers rehearsal with an ID-only schema and all modeled relationships.
CREATE TABLE original_event (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  scheduled_at timestamptz NOT NULL, body text);
CREATE TABLE original_queue (id bigint PRIMARY KEY,
  event_id bigint NOT NULL UNIQUE REFERENCES original_event(id) ON DELETE CASCADE);
CREATE TABLE original_push (id bigint PRIMARY KEY,
  event_id bigint NOT NULL REFERENCES original_event(id) ON DELETE CASCADE);
CREATE TABLE original_history (id bigint PRIMARY KEY, queue_id bigint NOT NULL);
INSERT INTO original_event(scheduled_at,body) VALUES
  ('2026-09-01', 'expired'), ('2026-10-01','keeper');
INSERT INTO original_queue VALUES (1,1),(2,2);
INSERT INTO original_push VALUES (1,1),(2,2);
INSERT INTO original_history VALUES (1,1),(2,2);
-- In this rehearsal the transaction holds all writers out during copy and swap.
LOCK TABLE original_event, original_queue, original_push, original_history IN ACCESS EXCLUSIVE MODE;
CREATE TABLE replacement_event (LIKE original_event INCLUDING ALL);
CREATE TABLE replacement_queue (LIKE original_queue INCLUDING ALL);
CREATE TABLE replacement_push (LIKE original_push INCLUDING ALL);
CREATE TABLE replacement_history (LIKE original_history INCLUDING ALL);
INSERT INTO replacement_event OVERRIDING SYSTEM VALUE
SELECT * FROM original_event WHERE scheduled_at >= '2026-09-15';
INSERT INTO replacement_queue SELECT q.* FROM original_queue q JOIN replacement_event e ON e.id=q.event_id;
INSERT INTO replacement_push SELECT p.* FROM original_push p JOIN replacement_event e ON e.id=p.event_id;
INSERT INTO replacement_history SELECT h.* FROM original_history h JOIN replacement_queue q ON q.id=h.queue_id;
-- LIKE does not copy foreign keys; rebuild explicitly.
ALTER TABLE replacement_queue ADD FOREIGN KEY(event_id) REFERENCES replacement_event(id) ON DELETE CASCADE;
ALTER TABLE replacement_push ADD FOREIGN KEY(event_id) REFERENCES replacement_event(id) ON DELETE CASCADE;
SELECT setval(pg_get_serial_sequence('replacement_event','id'), (SELECT max(id) FROM original_event), true);
ALTER TABLE original_event RENAME TO retained_old_event;
ALTER TABLE replacement_event RENAME TO original_event;
SELECT assert_true(EXISTS(SELECT FROM pg_constraint WHERE conrelid='original_queue'::regclass
  AND confrelid='retained_old_event'::regclass),
  '14 Rename does not redirect existing foreign keys', 'Old queue still references the old table object');
ALTER TABLE original_queue RENAME TO retained_old_queue;
ALTER TABLE replacement_queue RENAME TO original_queue;
ALTER TABLE original_push RENAME TO retained_old_push;
ALTER TABLE replacement_push RENAME TO original_push;
ALTER TABLE original_history RENAME TO retained_old_history;
ALTER TABLE replacement_history RENAME TO original_history;
SELECT assert_true((SELECT count(*)=1 FROM original_event) AND
  (SELECT count(*)=1 FROM original_queue) AND (SELECT count(*)=1 FROM original_push) AND
  (SELECT count(*)=1 FROM original_history),
  '15 Copy-keepers preserves related keep-set', 'Synthetic policy retains children and soft history only for retained parents');
INSERT INTO original_event(scheduled_at,body) VALUES ('2026-10-01', 'post-cutover write');
SELECT assert_true((SELECT max(id)=3 FROM original_event) AND
  NOT EXISTS(SELECT FROM retained_old_event WHERE id=3),
  '16 Sequence continuity and stale rollback copy', 'Generated ID exceeds old high-water mark; old table lacks new write');

SELECT expect_error(
  $$ALTER TABLE event SPLIT PARTITION event_old INTO (
    PARTITION event_a FOR VALUES FROM ('2026-09-01') TO ('2026-09-01 12:00Z'),
    PARTITION event_b FOR VALUES FROM ('2026-09-01 12:00Z') TO ('2026-09-02'))$$,
  '42601', '17 Screenshot SPLIT PARTITION syntax rejected');

-- PostgreSQL 17 changed identity inheritance on direct-to-leaf operations.
DO $$ BEGIN
  IF current_setting('server_version_num')::integer >= 170000 THEN
    INSERT INTO event_live(scheduled_at, body) VALUES ('2026-10-01 16:00Z','direct leaf');
    PERFORM assert_true(EXISTS(SELECT FROM event_live WHERE body='direct leaf' AND id IS NOT NULL),
      '18 Direct leaf generated identity', 'PG17: omitted ID generated on leaf');
    PERFORM expect_error(
      'INSERT INTO event_live(id,scheduled_at) VALUES (999,''2026-10-01 17:00Z'')',
      '428C9', '19 Direct leaf explicit identity rejected');
  ELSE
    PERFORM expect_error(
      'INSERT INTO event_live(scheduled_at,body) VALUES (''2026-10-01 16:00Z'',''direct leaf'')',
      '23502', '18 Direct leaf does not inherit identity');
    INSERT INTO event_live(id,scheduled_at) VALUES (999,'2026-10-01 17:00Z');
    PERFORM assert_true(EXISTS(SELECT FROM event_live WHERE id=999),
      '19 Direct leaf accepts explicit identity', 'PG16: ALWAYS on parent not inherited by direct leaf');
  END IF;
END $$;

SELECT test, detail FROM evidence ORDER BY test;
SELECT count(*) AS passed_checks FROM evidence;
ROLLBACK;
