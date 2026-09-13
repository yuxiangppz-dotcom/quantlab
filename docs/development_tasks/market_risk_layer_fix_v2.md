# Market risk layer fix card v2 (PR #122 review remainder)

Direct commission continuation. Branch `codex/market-risk-layer-v1` in the
isolated clone `/home/administrator/projects/quantlab-zcode-risk`; the Codex
directory, its review outputs, data, fees, shared backtest core and automation
are untouched. Reviewer-verified items (R1-R3, and R4 cap/fingerprint/
long-only boundaries) are not reworked.

- Fix base (the re-reviewed commit): `2576fd116ee4d52d5f8901e0aa3cc794bf89445a`
- Defect re-verified on that exact head before this card: calling the public
  helper `decide_constant_cap(inputs, "V")` returns `status=ok, cap=0.5`
  carrying rule V's real config fingerprint — a misuse surface the unified
  entry does not have (it correctly returns unknown for V without returns).

## Scope (one bounded fix, no rule or experiment expansion)

`decide_constant_cap` accepts only `C80` and `C50`. Any other rule id string
(V, M, D, VM, VMD, unknown ids) and any illegal type rejects with ValueError
before computing anything — the helper must never mint a decision for a rule
it does not implement. The unified `evaluate_market_risk_rule` routing and all
existing behavior stay unchanged.

## Acceptance

Parametrized regression tests cover rejection of V, M, D, VM, VMD, an unknown
id and illegal types (None, int, bool, float), plus confirmation that C80
returns 0.8 and C50 returns 0.5 with their frozen fingerprints, and that the
unified entry keeps its original behavior for the same inputs. Full
`uv run pytest`, `uv run ruff check .`, `git diff --check` must pass. Commits
append; nothing handed to review is rewritten; the PR stays open for Codex to
merge after re-review.
