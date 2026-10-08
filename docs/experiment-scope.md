# Experiment scope

## Starting assumptions

The reference workload contains a request table and a delivery queue, each currently identified by a single bigint primary key. Queue records refer to request records through a cascading foreign key. The subsequent sanitized work-side responses describe push rows cascading from the parent, soft queue-history links, and template cascades. The queue is mapped one-to-one in EF but lacks a matching database unique constraint. [Phase 8](phase-8.md) models these supplied facts.

The source brief estimates roughly 100 million expired request rows, HTML-heavy row content, and a delete backlog constrained by write throughput. These are workload inputs supplied for design, not measurements made by this lab.

## Questions to answer

1. Is `send_after` the right retention clock, including future scheduling, retries, and late arrivals?
2. Does monthly partitioning satisfy the intended interpretation of 30-day retention, or are smaller intervals required?
3. What key and application changes are needed to partition both tables while preserving referential integrity?
4. Must related records share a request partition boundary, even when delivery timestamps differ?
5. Can export, verification, restore, and retirement be coordinated safely under failures and concurrent activity?
6. Which Aurora engine and extension versions support the proposed SQL and maintenance approach?
7. What is the least disruptive migration route for the existing backlog?

## Evidence expected

Use a small synthetic dataset to demonstrate successful paths and failures. Record exact engine and extension versions, partition boundaries, export results, restore comparisons, and teardown results. Distinguish locally checked syntax from behavior observed on AWS. A small lab cannot prove production throughput or a 100-million-row migration duration.

## Boundaries

Do not use production data, change existing databases, import credentials into the repository, or claim seven-year compliance from a short-lived S3 experiment. Production identity compatibility, business retention policy, full application query coverage, and migration cutover require separate validation. Supplied relationship facts are already incorporated into the sample.
