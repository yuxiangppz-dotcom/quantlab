# Reference-plan economic cutoff — Issue #42

Direct single-agent user commission. Starting exact clean/pushed master:
`b2927adbd76c91dedb4a58dd3e5e9fcf192d3a7c`; post-merge CI `34496203497` passed.
Branch: `codex/reference-plan-economic-cutoff`.

Extend the existing fill cutoff to imported cash-flow effective dates. A flow
occurring on or after the intended session must not fund an old reference plan.
Reject a complete account basis dated later than the intended session. Preserve
existing intended-day opening snapshots, and distinguish late reports from late
economic facts. This is conservative current-state planning, not historical replay.

Reject before writing plan artifacts. Display when an old saved plan is bound to
an earlier Daily snapshot. No ledger, price, fee, T+1 or frozen financial change;
no provider calls, real account writes, model training or orders.

Verify synthetic deposits/withdrawals at the boundary, timezones, delayed reports,
opening basis dates and no plan writes on rejection, then full pytest/Ruff/diff
and optional runtimes. Commit/push, green PR CI, merge and verify master. Report
findings, tests, exact HEADs, deviations and remaining limits in PR/delivery notes.

Verification: five new regression cases failed against the original code because
it produced the stale plan, while the three compatibility cases passed. After
the fix all eight pass, including both CNY directions and an equivalent UTC date
boundary. Full regression: 1,217 passed, 2 optional skipped; enabled optional
runtimes: 2 passed; Ruff and diff-check pass. No test assertion was relaxed.
No scope deviation beyond the planned stale-plan UI disclosure. Historical
account replay and full performance eligibility remain the next separate task.
