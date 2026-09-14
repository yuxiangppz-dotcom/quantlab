# S5-B v1.2 self-review

## Findings corrected

1. A parametrized sector test used `dict.get()` and accidentally marked every
   case without that key as incomplete history. The condition now distinguishes
   an absent key from an explicitly missing value.
2. Covered membership rows are now checked for both resolved-sector and
   expected-sector consistency, so an internally inconsistent supplied audit
   row fails closed.
3. Direct regressions cover missing membership, duplicate sector identity,
   mismatched membership identity, and stock `unknown` / `failed` states.

## Verified properties

- sector and stock gates accept only ready or confirmed states;
- unresolved PIT membership stays unknown and mismatch is ineligible;
- research eligibility stays fail-closed;
- confirmed states rank before ready states, with deterministic feature and id
  tie-breaking independent of input order;
- max-name truncation and exact residual cash include a valid all-cash result;
- outputs remain research-only with no performance claim or broker authority;
- S5-A, S5-B feature thresholds and S4 PR #126 are unchanged.

Targeted S5-B suite: 58 passed. Ruff passed. Full repository CI remains required
before merge.
