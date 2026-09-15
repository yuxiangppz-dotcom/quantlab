# S4 admission fix card v2 (PR #126 second-review remainder R2-R5)

Direct commission continuation on `codex/s4-first-replay-admission-v1`.
Reviewed base `2afc7c39af3af1158eb8b7ae20cc46686564ece3`; mainline `52c4f10`;
append-only; Codex directory read-only; automation paused; zero downloads,
canonical writes, models, economic paths, orders, promotions. Closed items
(R1 corporate values, 7+13 volumes, ranking, pending-order dates, frozen
fingerprint validation, overwrite rejects, exclusive outputs, R6 checks) are
not reworked. The six real dates keep their limited supplier-basis full-day
suspension conclusion — Codex re-read the real bodies (code=0, items=[]) and
empty timings — and the prior20 window, zero-fill and backfill rules stay
untouched.

## R2 residual (P1): parse the body; keep missing evidence unknown

`verify_attempt` now verifies the archival chain (result.intent_fingerprint
== intent.fingerprint; result.artifacts/wire sha256 present and equal to the
actual body bytes; server_code 0) and parses the real body JSON: code == 0,
data.items an empty list, msg not an error. Any conflict, missing binding or
invalid body keeps `response_verified_empty = False` with the reason.
`check_prior20_gaps` separates daily coverage from bar presence: a missing
directory, empty directory, missing month partition or missing required
columns yields `local_coverage = incomplete` and `local_bar_present = None`,
which can never support a full-day claim; same-day non-S or resume records
keep refusing. Only verified-empty response + confirmed no-bar-under-full-
coverage + S-with-empty-timing classifies as the supplier-basis suspension.

## R3 residual (P2): persist the full source binding

Every consumed file — intents, results, response bodies, daily and
suspension partitions, and the suspension source-record ids — is bound in
persisted per-root `InputBinding` entries and exported into the report
manifest with its root label; both bindings re-verify at the end. Bodies are
read through the binding so verification and parsing consume the same bytes.
`completed.json` records the manifest-bearing report hash so the completion
mark traces to the binding.

## R4 residual (P1): preflight matches kernel semantics

`necessary_field_gaps` now also flags: participation None; minimum
commission None; prior20_asof after the decision date; evidence_date not the
execution date; next_session not after it; limit/bar ordering violations
(down<=low<=close<=high<=up); negative session amount; non-finite decimal
strings ('NaN'); float prior20_sessions; missing scenario ids; missing
rules/fees bodies. Legal zeros (volume, amount, minimum commission) stay
legal known non-tradability and are never flagged as positive-number gaps.
Regressions use the reviewer's counterexamples plus the legal-zero boundary.

## R5 residual (P1): the verifier must not parrot labels

`scripts/s4_admission_independent_verify.py` is restructured around a
`verify()` function with explicit directories and independent logic:
canonical-fingerprint recomputation over the sealed bodies compared to the
frozen identities and the report manifest; direct reads of intents, result
metadata, response bodies, daily partitions and suspension partitions to
independently re-derive each prior20 classification; per-instrument context
diffs against the sealed plan allowing only the seven volume fills (original
None) and the entry-day corporate flag; per-instrument fields/open_gaps
consistency; the proof records the exact hashes of the verified package,
report, completed marker, resolved manifest and the verifier code itself
into a fresh proof file that never overwrites; success and failure both
persist. The test suite drives this function over hand-built worlds where
tampered fields, a dropped reason, an altered suspension source, changed
plan content, a stale proof and missing response/daily evidence must each
fail.

## P3 docs

`docs/s4_first_replay_inputs_zh.md` updated: v3 output directory, keyword
arguments for the verifier, a complete `Checkpoint.start` example that
passes `pending_orders=build_first_pending_orders(package)`, read-only
sealed-directory note, current output list, proof scope and the
supplier-basis wording.

## Budget and delivery

The v3 package is generated exactly once into a new directory and verified
once, after the regressions pass; v1/v2 and their proofs stay as-is. Full
`uv run pytest`, `uv run ruff check .`, `git diff --check` at the final
head with PR CI verified. No new scope after the time box; handover states
actual progress.
