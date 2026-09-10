# Mainline task: Forward Shadow temporal admission v2

Authority: direct user commission for architectural takeover, independent review,
one bounded correctness task, tests, push, PR, CI, merge and next-task publication.
This is outside the paused agent loop. Expected starting HEAD:
`41dd5ee3dd41db2b5b262d263194d01dc19bd09c`, clean and pushed.

## Problem and scope

At the starting HEAD, a synthetic January 26 signal generated March 1 was counted
as complete forward evidence. Editing `created_at` did not invalidate its hash.
Close this evidence-admission gap before expanding research or account features.

- Bind prediction and source observation timestamps in v2 artifacts.
- Apply a conservative, explicit same-signal-day registration window.
- Preserve v1 and late artifacts as ineligible evidence; no migration or deletion.
- Exclude ineligible predictions in evaluation, summary, paired diagnostics and
  strategy readiness; keep exclusion visible to users.
- Preserve first-publication idempotency and reject conflicting v2 inputs.
- Preserve all financial/accounting, portfolio and label calculation semantics.

## Verification

Synthetic checks cover timestamp tampering, rehashed admission forgery, invalid
timestamps, Shanghai/UTC boundaries, source ordering, stale snapshots, legacy
complete evaluations, duplicates, conflict preservation, retries and paired identity.
Run focused regressions, full `uv run pytest -q`, `uv run ruff check .`,
`git diff --check`, and the actual optional LightGBM/Qlib smoke.

No provider request, Canonical write, real account journal mutation, new research
experiment, broker action, strategy promotion or performance claim is authorized.

GitHub Issue/PR publication and remote CI require an authenticated GitHub API or
browser session; SSH Git access alone does not expose those operations. Track the
actual publication state in the takeover review rather than inventing an Issue ID.
