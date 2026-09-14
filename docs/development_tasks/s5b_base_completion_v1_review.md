# S5-B v1 self-review

## Scope and semantics

- S5-A files are unchanged.
- A new low alone cannot produce `failed`.
- Failure and incomplete-base reasons are independent and deterministically ordered.
- Missing inputs and incomplete/unknown history fail closed to `unknown`.
- Breakout confirmation requires both price and volume.
- Results are claim-neutral and carry no broker authority.

## Review correction

The first draft represented only the 10-session new-low count and a reclaim
boolean. Review found that this did not preserve the task card's 20-session
context or reclaim speed. `new_low_count_20` and `reclaim_sessions` were added,
with consistency, missingness, boundary and slow-reclaim tests.

## Verification

- targeted synthetic tests: 26 passed;
- Ruff on the new module and test: passed;
- full repository CI is required before merge.

No outcome data, provider, Canonical, portfolio, account or execution path was
used.
