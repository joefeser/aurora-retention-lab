# Phase 5: query shapes and bounded recovery

Run `python3 scripts/run-recovery.py` against the recorded lab. This fixture uses 100 expired parent/queue pairs and one live pair on Aurora 17.9. It retains the 1 ACU ceiling and uses a fresh synthetic schema. It is a recovery experiment, not a production recovery service.

## Cases exercised

- Parent and queue UPDATE by ID, versus ID plus a constant partition key, while expired leaves have SHARE locks.
- Parent DELETE with cascading queue deletion, with and without the partition key.
- An expired UPDATE that must hit the fence.
- Export followed by explicit transaction rollback, proving source preservation and the survival of non-transactional S3 objects.
- A committed expired-row change after the fence is released, proving the previous export has become stale.
- A new locked attempt with new export keys and full restore comparison.
- A held reader transaction preventing the parent access-exclusive lock; the fixture expects `55P03`, preserves the verified source, releases the reader, and retries.
- An injected exception after dropping the expired queue leaf, proving transactional rollback restores its relation and data before the successful retirement attempt.
- Fresh post-commit requests confirming the retired relations are absent, the live pair is unchanged, and the cascade FK remains validated.

The probe function catches only lock timeout or its own deliberately injected SQLSTATE. Successful mutation probes are rolled back inside a subtransaction; other SQL errors fail the run. The successful recovery path never promotes an abandoned export: it reacquires the fence, re-exports, restores, and compares both relations before destructive SQL.

## Results

All 12 recovery assertions and seven query probes completed on Aurora 17.9 in 37.787 seconds on October 8, 2026. All four exports passed SSE-KMS checks. Evidence: `evidence/recovery.json`.

| Query shape under the expired-leaf fence | Observed outcome |
| --- | --- |
| Live parent UPDATE by ID only | Blocked, `55P03` |
| Live parent UPDATE with ID and constant partition key | Admitted, then deliberately rolled back |
| Live queue UPDATE by ID only | Blocked, `55P03` |
| Live queue UPDATE with ID and constant partition key | Admitted, then deliberately rolled back |
| Live parent cascading DELETE by ID only | Blocked, `55P03` |
| Live parent cascading DELETE with ID and constant partition key | Admitted, then deliberately rolled back |
| Expired parent UPDATE | Blocked, `55P03` |

This identifies an application compatibility requirement: the tested ID-only write shapes can block even when the intended row is live. Adding a constant partition predicate changed the observed behavior, but prepared/generic plans need their own tests. The earlier insert/read result must not be described as unrestricted online archival.

The abandoned objects remained in S3 after rollback. After a late source update, exact comparison detected the stale archive. Fresh exports matched; a held reader prevented retirement without source loss; the injected partial-DDL failure restored the child partition and contents; the subsequent retirement preserved the live pair and its FK.

## Recovery boundary

Explicit rollback models a **confirmed** cancellation. It does not simulate network loss, process death, Aurora failover, an unknown commit result, or automatic restart. The partial-DDL failure uses a caught subtransaction exception, not a disconnected session. Fresh post-commit reads demonstrate reconciliation after a confirmed commit, not resolution of an ambiguous commit acknowledgement.

The fixture's query probes use literal partition keys through dynamic SQL, not application prepared statements or EF. One incoming queue FK is modeled; push and soft-history recovery are not exercised here. Those relations were included in the earlier concurrent archival fixture. Successful synthetic probes are not authorization to apply this protocol to work tables.

Before a production controller can retry, it needs durable attempt identity, exact table/partition identity, archive metadata and validation state, exclusive ownership, and a way to reconcile whether retirement committed. An uncertain outcome must stop for reconciliation; replaying a DROP or accepting an old manifest blindly is unsafe. Released fences invalidate assumptions that archived contents still match the source.

Each request is guarded by a 15-minute run deadline; running requests are not cancelled by that guard. Transactions use a 250 ms lock timeout and 35-second statement timeout. S3 objects and committed fixture data remain until lab teardown. No new infrastructure is added. See the [canonical lab lifecycle](lab-lifecycle.md) for the owner-confirmed teardown schedule. The reminder does not delete resources automatically.
