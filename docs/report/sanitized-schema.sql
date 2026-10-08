-- Derived from the supplied sanitized parent/queue DDL and relationship review.
-- Supporting FK indexes and minimal push/history/template payloads are explicit fixture choices.
CREATE SCHEMA messaging_sample;

CREATE TABLE messaging_sample.notification_template (
    id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name text NOT NULL);

CREATE TABLE messaging_sample.notification_event (
    id bigint GENERATED ALWAYS AS IDENTITY,
    recipient_id varchar(10) NOT NULL,
    message_text varchar(1000),
    subject_text varchar(100) NOT NULL,
    scheduled_at timestamptz NOT NULL,
    expires_at timestamptz,
    notification_template_id integer NOT NULL,
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    external_ref_id uuid NOT NULL DEFAULT gen_random_uuid(),
    send_state smallint NOT NULL DEFAULT 0,
    content_body varchar(10485760),
    push_notification_text text,
    sms_notification_text text,
    email_notification_text text,
    target_time timestamptz,
    metadata jsonb,
    remaining_minutes integer NOT NULL DEFAULT 0,
    PRIMARY KEY(id),
    FOREIGN KEY(notification_template_id) REFERENCES messaging_sample.notification_template(id) ON DELETE CASCADE);

CREATE TABLE messaging_sample.delivery_queue (
    id bigint GENERATED ALWAYS AS IDENTITY,
    recipient_id varchar(10) NOT NULL,
    notification_template_id integer NOT NULL,
    notification_event_id bigint NOT NULL,
    message_text text,
    subject_text varchar(100) NOT NULL,
    send_details jsonb NOT NULL,
    all_targets_attempted boolean NOT NULL,
    expires_at timestamptz,
    scheduled_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL,
    send_state smallint NOT NULL,
    content_body varchar(10485760),
    push_notification_text text,
    sms_notification_text text,
    email_notification_text text,
    metadata jsonb,
    PRIMARY KEY(id),
    FOREIGN KEY(notification_event_id) REFERENCES messaging_sample.notification_event(id) ON DELETE CASCADE,
    FOREIGN KEY(notification_template_id) REFERENCES messaging_sample.notification_template(id) ON DELETE CASCADE);

CREATE TABLE messaging_sample.push_delivery (
    id bigint GENERATED ALWAYS AS IDENTITY,
    notification_event_id bigint NOT NULL,
    body varchar(200) NOT NULL,
    PRIMARY KEY(id),
    FOREIGN KEY(notification_event_id) REFERENCES messaging_sample.notification_event(id) ON DELETE CASCADE);

CREATE TABLE messaging_sample.send_history (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    delivery_queue_id bigint NOT NULL,
    detail text NOT NULL);

CREATE INDEX ON messaging_sample.send_history(delivery_queue_id);

CREATE INDEX ON messaging_sample.delivery_queue(notification_event_id);

CREATE INDEX ON messaging_sample.push_delivery(notification_event_id);
