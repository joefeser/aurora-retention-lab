# Disposable lab lifecycle

The AWS lab must use dedicated, identifiable resources and synthetic data. Implementation belongs on the integration branch.

Before provisioning, establish the authenticated account, region, resource names, expected cost, and cleanup scope. Prefer infrastructure managed as a single dedicated stack and record resources that live outside it. Keep credentials and generated account-specific inventory out of Git.

The original retention experiment window ended **October 9, 2026 at 1:00 PM Central (America/Chicago)**. Its stack reached `DELETE_COMPLETE`, and the database, archive bucket, managed secret and cluster snapshots were checked absent. Historical phase documents refer to that completed window.

The October 12 [major-upgrade timing extension](upgrade-timing.md) uses new isolated resources for each case. Successful cases delete their own stack and upgrade snapshots immediately after saving evidence. Failed or interrupted cases retain a private inventory for inspection and explicit cleanup; inspect `.lab/upgrade-timing/*/inventory.json` before ending work. The old reminder does not cover these new resources. Neither a reminder nor a deletion request proves resources or charges have stopped.

The implementation must include a teardown procedure for the database, compute or network helpers, S3 objects and versions, secrets, and any separately created resources. Confirm deletion from AWS and record leftovers. Temporary archive objects must remain deletable; this lab must not enable irreversible seven-year retention locks.

Do not remove unrelated account resources. Preserve only public-safe, synthetic experiment evidence in the repository.
