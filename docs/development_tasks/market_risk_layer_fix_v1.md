# Market risk layer fix card v1 (PR #122 review R1-R4)

Direct commission continuation. Branch `codex/market-risk-layer-v1` in the
isolated clone `/home/administrator/projects/quantlab-zcode-risk`; the Codex
working tree and review outputs are read-only references. Reviewer mainline
`fac688c2b7824b9ced173ecafb24f654f1caecca` is left untouched; this branch is
not rebased and no handed-over commit is rewritten — fixes append as new
commits and the PR stays open.

- Fix base (the reviewed commit): `a3221d3b86221d91238f9d03d74ee1f5c2e638bb`
- All four defects were re-verified present on that exact head before this
  card was written: silent auto-initialization at `decide_drawdown_governor`,
  same-day re-advancement, the unguarded `annualized == 0.0` overflow path,
  membership-style coefficient checks, and no temporal validation in
  `apply_risk_cap`.
- Scope: only `market_risk.py`, `risk_target_adapter.py`, their tests,
  `docs/market_risk_layer_v1.md` and this card. No engine, kernel, scheduler,
  fee, data-fact, ranking or UI change. No downloads, canonical writes, sealed
  reruns, training, historical return conclusions, orders or promotions.

## R1 — temporal contract for the adapter

`apply_risk_cap` validates time before translating. A target whose `as_of` is
after the decision date is rejected: an older risk signal must never relabel a
newer intention. A target dated before the decision date is the supported
"existing holdings assessed daily" case: weights are projected to the decision
date, the output target carries `as_of == decision.decision_date`, and the
result reports `signal_as_of` preserving the original selection date. Same-day
input keeps returning a target dated that day. Cap zero in the projection case
still yields explicit all-cash at the decision date. The
next-common-session execution rule is unchanged (it lives in the decision).

## R2 — dated, identified, idempotent drawdown state

`RiskState` now carries `series_id`, `last_decision_date` and a version, with
exact-type Decimal checks (bool/float equality no longer passes). First use
requires an explicit `RiskState.initial(nav, series_id)`; a running decision
that finds no state returns unknown `state_unavailable` — a restart that lost
state is never interpreted as a fresh start. A state from another NAV series,
a future `last_decision_date`, or a mismatched version rejects. A state whose
`last_decision_date` equals the decision date rejects: one transition per
date; callers that need retry semantics persist the pre-decision state and
rewind. Same-day NAV changes therefore cannot stack recoveries. The
transition, thresholds, staged recovery and high-water persistence semantics
(from the frozen card, including the 12% band precedence) are unchanged; no
new parameter variants. VM/VMD evaluate D through the same contract: a wiring
error raises through the composite, and repeated composite calls cannot
advance D twice.

## R3 — overflow is unknown, never "known all-cash"

V computes the mean and squared deviations with `math.fsum`, treats
`OverflowError` and any non-finite intermediate (mean, sum of squares,
variance, annualized estimate) as unknown `nonfinite_volatility`, and guards
the final cap to a finite Decimal within [0, 0.8]. Inputs that are
individually finite but jointly unrepresentable (60 × 1e308, or alternating
±1e308) return unknown with no cap. ddof=1, 252-day annualization, zero
volatility → 0.8, and warmup gaps are unchanged.

## R4 — boundary validation on public objects and every adapter branch

`RiskDecision` now requires, for ok decisions, an exact-type finite Decimal
cap within [0, 0.8], and a `config_fingerprint` equal to the frozen
fingerprint of its `rule_id`. `RiskState` coefficient/high-water-mark use
exact-type Decimal validation. `apply_risk_cap` validates the target before
every branch — including cap-zero and unchanged shortcuts: long-only weights,
no single weight above 1, non-negative cash, gross exposure at most 1 — so an
overweight/negative-cash portfolio can no longer ride a shortcut to ready.
Unknown stays not-ready, lower-risk targets are never upsized, and a valid
cap-zero still returns `positions=()` with `cash_weight=1.0`.

## Acceptance

New regression tests for every item above (temporal accept/reject/project,
same-day rejection, series/version/future-state rejection, missing-state
unknown, extreme-value unknowns, type/range rejections, shortcut-bypass
rejections) are added without deleting or weakening any existing assertion.
Full `uv run pytest`, `uv run ruff check .`, `git diff --check` must pass.
This layer still only produces position intentions; unsellable holdings remain
the execution ledger's authority, and no drawdown or return is guaranteed.
