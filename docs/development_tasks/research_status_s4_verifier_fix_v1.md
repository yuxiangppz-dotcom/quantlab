# Research-status legacy compatibility + S4 verifier frozen-identity card

Direct commission on a fresh branch from master
`ad5e16147929e8f6a35ede10936086c1adf691d6`. Append-only; Codex tree
read-only except authorized read-only inspection and command runs;
automation paused. Zero downloads/canonical writes/models/economic
paths/orders.

## A. Research-status legacy-artifact compatibility

Reproduced in the Codex workspace: `research-status` crashes in
`build_evidence_catalog(strict=True)` on
`execution_readiness_v0_1/20260906T173554/summary.json`, whose payload
carries `experiment_schema` (a legacy experiment summary) and no
`evidence_schema` at all — it is an identifiable legacy research product,
not a corrupt current-format artifact.

Fix, preserving the strict entry semantics for other consumers:
1. `build_evidence_catalog` classifies unparseable summaries in non-strict
   mode as either `legacy_experiment_summary` (payload carries a non-empty
   `experiment_schema`) or `malformed_evidence` — the issue message states
   which, and no eligibility is invented for either.
2. `build_research_status` composes with `strict=False`, adds an
   `incompatible_artifacts` section (path, classification, reason) and an
   overall `overall_status` field; text and `--json` both render it.
3. Exit-code convention in `__main__`: 0 = status composed (legacy notes
   allowed), 1 = corrupt current-format evidence present, 2 = the status
   could not be composed at all. Tested.

No old file is modified or deleted; no legacy artifact gains evidence
eligibility; a corrupt current-format artifact is never shown as valid.

## B. S4 verifier frozen identity for profiles

`FROZEN` in the independent verifier lacks `profiles`, so default calls
crash with `KeyError: 'profiles'`. The frozen identity is derived
independently from the sealed `profiles.json` body (canonical fingerprint
recomputed, cross-checked against the recorded fingerprint
`6e5fbc67e4d8169dfa5797eaca313d525c7106fafb42af282c188abe6845f0de`) and
added to the default map. All existing checks (three-base recompute,
manifest v2, six frozen gap identities, byte counts, bound_files) stay.
A legacy package gets an explicit all_ok=false under the current contract
instead of a crash.

## C. Verification

New regressions: valid evidence coexisting with a legacy summary (status
shows both), a corrupt current-format artifact (never valid), text/JSON
agreement, exit-code convention, the S4 default frozen map containing
three identities, default-call no-crash on legacy packages, and tamper
failures. A local real-file connectivity pass is reported separately from
synthetic tests.
