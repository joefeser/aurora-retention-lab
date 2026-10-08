# Disposable lab lifecycle

The AWS lab must use dedicated, identifiable resources and synthetic data. Implementation belongs on the integration branch.

Before provisioning, establish the authenticated account, region, resource names, expected cost, and cleanup scope. Prefer infrastructure managed as a single dedicated stack and record resources that live outside it. Keep credentials and generated account-specific inventory out of Git.

The intended experiment window ends October 9, 2026 (America/Chicago). A chat reminder will prompt teardown; a reminder is not an automatic resource expiry or a guarantee that billing has stopped.

The implementation must include a teardown procedure for the database, compute or network helpers, S3 objects and versions, secrets, and any separately created resources. Confirm deletion from AWS and record leftovers. Temporary archive objects must remain deletable; this lab must not enable irreversible seven-year retention locks.

Do not remove unrelated account resources. Preserve only public-safe, synthetic experiment evidence in the repository.
