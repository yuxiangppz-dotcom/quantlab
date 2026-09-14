# S4 admission fix card v5 (date-unknown, manifest identity, required-gap sweep)

Direct commission continuation on `codex/s4-first-replay-admission-v1`.
Reviewed base `3d022348c3ab4b62fdaa639f0cbd44fb0f962eca`; mainline
`eb6a832`; append-only; Codex tree read-only; automation paused. The real
input-package generator is NOT run this round (v1-v5 and all proofs stay);
whether a fresh proof is needed is decided after code review.

## A. Date-unknown handling and one contract for both views

`rules.effective_from` / `effective_through` / `fees.effective_from` /
`fees.effective_through` set to None currently pass `necessary_field_gaps`
while the assembly raises. Missing required start/end dates are now
reported as `missing (unknown)`; invalid strings stay `not a valid ISO
date`; relations run only on parseable pairs. TestContractConsistency is
repaired: the base input is genuinely complete (no gaps, assembles), each
mutation asserts its own concrete finding, and unknown/invalid/legal-zero
are separated.

## B. Manifest identity: a key must be that exact file

The manifest check is rebuilt: every key must parse as `root:relative`,
resolve to exactly the entry's `path`, live under the declared root, and
match the recomputed hash (exact match, no startswith). The daily and
suspension partition coverage enumerates the partition files actually
consumed for each gap date and requires a one-to-one manifest entry.
`bound_files` entries conflicting with `files` fail. Regression: all keys
present, all hashes correct, but every path swapped to one existing
canonical file must fail.

## C. Required gaps re-derived from the frozen contract

The required per-instrument unknown set comes from the contract, not from
the report: historical-identity/signal-eligibility is always required;
market_open, prior20 and the two additional-fee components are required
whenever their context values are None; session volume when None. The
producer now emits stable exact field ids for the additional-fee unknowns
(replacing the free-text context.* entries for those two); the verifier
matches field identities exactly and re-derives per-instrument
open_gaps, the gap summary and the verdict/stop date, distinguishing
"still stopped" from "gap report complete and correct". Regressions:
omitting the identity entries, dropping one fee gap, wrong gap counts and
wrong instrument attribution each fail.

## D. Honest v5 record

The v5 record states the real attempts (two generations — first deleted;
two verifications — first `all_ok=false` on the fee-field matching defect
fixed this round). Under the rebuilt verifier the existing v5 proof is no
longer valid evidence; re-running the generator for a v6 proof is a
separate decision this round does not take.

## E. Budget

Code fixes, hand-built regressions and documentation only. No real
package regeneration, downloads, canonical writes, models, economic
paths, orders or promotions. Full pytest/ruff/diff at the final head with
PR CI verified.
