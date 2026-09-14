# S5 v1.2 Self-Review

Scope: Issue #137, branch `chatgpt/s5-membership-readiness-v1`.

## Semantics

- The merged S5 v1 kernel and v1.1 feature formulas are untouched.
- The audit population is explicit and non-empty; duplicate `(instrument_id, as_of)` requirements are rejected rather than counted twice.
- Membership intervals are inclusive. No active evidence is `missing`; any active unverified fact makes the row `unverified`; multiple verified sector identities are `conflicting`; a unique verified identity that differs from an explicit expected sector is `mismatch`; otherwise the row is `covered_verified`.
- Multiple agreeing PIT-verified sources are allowed and all source ids remain visible.
- Empty evidence cannot become successful coverage; it produces missing rows and a blocked audit.
- A decision date is fully covered only when every required row on that date is `covered_verified`.
- Annual coverage uses the complete predeclared denominator; no missing row is removed from the denominator.
- Sector summaries retain the expected sector denominator (or the resolved sector when no expectation exists); unresolved rows remain an explicit `None` group. Source summaries count affected requirements once per source even when duplicate evidence rows agree.

## PIT / fingerprint

- Facts starting after the latest required audit date cannot change an earlier audit or fingerprint.
- An open-ended fact and one whose end date lies after the required horizon are equivalent for this audit; later knowledge of a future termination date cannot rewrite earlier evidence.
- Historical source ids, historical sector identities, PIT verification flags and in-horizon effective endpoints remain fingerprint-bound.
- Evidence for instruments outside the required population is excluded so irrelevant rows cannot churn the audit identity.
- Requirement input order and evidence input order are normalized.

## Diagnostic freeze

The first S5 diagnostic protocol is machine-readable and outcome-free. It fixes S5 v1/v1.1, 100% membership coverage, `000985.SH`, 5/10/20 session forward horizons, four comparison families, signal-level diagnostics, one run budget and no post-result parameter rescan. It does not freeze a historical date range before real membership coverage is known; the eligible period must be selected from verified coverage and then frozen before loading S5 outcomes.

## Authority boundary

No provider call, download, Canonical write, S5 return/NAV, model fit, Forward registration, strategy promotion, account mutation or order is introduced. Results remain research evidence only. If historical membership data is unavailable locally, this task can merge the contract/tests but the actual data-readiness verdict remains pending rather than being fabricated.
