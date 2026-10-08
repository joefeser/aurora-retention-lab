# Sanitized request for work-side validation

Please assess whether parent-aligned time partitioning is compatible with the messaging application's actual schema and queries. Return sanitized structure and aggregate profiles only; do not include credentials, account identifiers, customer data, real message bodies, or production connection strings.

The lab established a constraint worth testing explicitly: under SHARE locks on expired leaf partitions, live UPDATE and cascading DELETE by ID only blocked, while tested SQL with an ID and constant partition timestamp was admitted. Live parent-routed inserts and reads continued. These were synthetic Aurora PostgreSQL 17.9 tests using literal SQL, not EF/prepared plans.

Please provide:

1. Exact database engine/version, ORM/provider versions, and whether writes use prepared/generic plans or connection pooling/proxy.
2. Sanitized parent, queue, push, and history DDL, indexes, incoming FKs, triggers, uniqueness rules, and any additional referencing tables. Identify soft relationships separately.
3. Actual query shapes for insert, update, delete, rescheduling, bulk jobs, and template-related cascades. Synthetic parameter values are sufficient. Can the parent's immutable retention timestamp accompany each write and FK?
4. Whether parent retention timestamps can change, which database/application rules enforce immutability, and whether children or history have independent retention requirements.
5. Aggregate parent/child row counts, age distribution, payload size percentiles, fanout percentiles/skew, index sizes, and observed delete throughput with its measurement conditions.
6. Typical and peak write concurrency, long-transaction duration, allowable writer blocking/cutover downtime, archive restore needs, and rollback expectations.

Please return a compatibility matrix with each query/relationship marked: compatible as-is, requires change, or unknown. Include evidence and an estimated application/migration effort range with assumptions. Treat a composite primary key as a change to the application identity contract; it does not enforce uniqueness of bare ID by itself. Do not propose production DDL execution as part of this review.
