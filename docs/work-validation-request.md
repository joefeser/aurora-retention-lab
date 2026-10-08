# Work-side validation coverage

The sanitized brief and both work-agent responses were already supplied. Do not request the entire schema/workload package again. They supplied the ID-only keys, queue ORM/database mismatch, push and template cascades, soft history, mutable queue scheduling, payload characteristics, and backlog/retention context used in this POC.

[Phase 8](phase-8.md) implements a runnable EF sample of that relationship contract. Its source is intentionally a minimal synthetic reconstruction, not a claim to contain every work table, column, index, or query. The following checklist distinguishes inputs from tests and decisions.

| Topic | Already supplied / implemented | Remaining specific validation or decision |
| --- | --- | --- |
| Keys and relationships | ID-only baseline, composite alternative, queue uniqueness mismatch, push/template cascades, soft history | Full application migration impact; business requirement for global bare-ID identity |
| Scheduling | Queue time is mutable; parent's time unmutated in reviewed code; sample adds immutable parent trigger | Business retention anchor, future-date range, independent child/history retention |
| ORM | Supplied key/navigation patterns reconstructed in EF; actual generated SQL captured | Work's exact SDK/EF/Npgsql versions and any unmodeled interceptors or raw writers |
| Plans and pooling | Local 16/17 cold and warmed pooled connections; custom/generic/automatic plan counters | Aurora wire protocol, proxy settings, representative partition count/data skew and resulting automatic plan choices |
| Queries | Sample insert/read, tracked update, bulk update/delete, rescheduling, parent/template cascades | Coverage comparison against each real writer and bulk job, rather than a general request for all schema again |
| Workload | Supplied backlog and payload estimates informed bounded fixtures | Measured fanout/size distributions, contention, WAL/vacuum/replication impact and throughput under stated conditions |
| Migration | Source-locked tiny copy/backfill, exact old-column comparison, identity advancement | Deployment-specific cutover window, grants/index inventory, online catch-up if needed, rollback after new writes |
| Archive | AWS synthetic export/restore receipts; local EF archive-retirement integration | Template archive fidelity, history policy, durable retry/commit reconciliation, restore SLA and long-term retention controls |

If asking work for follow-up, cite the specific untested behavior and request only that delta, with sanitized structure or aggregate measurements. Keep credentials, account details, customer data, real message bodies, and connection strings out of replies and the repository. No production DDL execution is requested by this checklist.
