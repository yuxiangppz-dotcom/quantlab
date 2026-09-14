# S4 admission fix card v3 (final integration + residual R2-R5)

Direct commission continuation on `codex/s4-first-replay-admission-v1`.
Verified baseline: master `eb6a832b99adc3149eecbccb8521beb78ecdca43` (S5 PRs
#129-#146 merged), PR head `a8ef07f5e31ec65ff4e2edc1cdd2babc13911dd2` OPEN;
the S5b readiness work on `chatgpt/s5b-diagnostic-readiness-v1` (d758589) is
Codex's and is not touched. Append-only; the approved master is brought in
by a normal merge commit; no rebase/amend/force; Codex tree read-only;
automation paused; zero downloads/canonical/models/paths/orders.

## A. necessary_field_gaps unified with the kernel contracts

All six Codex counterexamples currently slip through and must be listed:
participation "2" and "NaN" (range/finite), commission_rate "2" (rate range
0<=r<=1), additional_fee_fixed_fen -1 and prior20_amount_fen -1 (negative
invalid; zero stays legal known no-trade), next_session "2022-99-99" (real
ISO date parsing; the fixed date relations run only on parseable dates).
Restructured so int/float/str all reach the numeric validation, every
finding is reported in one pass, and unknown/illegal/legal-no-trade stay
distinct. Pure helpers are extracted so the checks and the assembly stay
one set of rules; frozen financial semantics unchanged.

## B. target-date coverage before any absence claim

`check_prior20_gaps` no longer treats "a month parquet with the needed
columns" as coverage of the target session. Coverage is full only when the
read partitions contain rows for exactly the target date (any instrument);
otherwise coverage is target-date-absent/missing and `local_bar_present`
stays None, blocking the full-day classification. Wrong-date partitions,
same-day conflicts and intraday/resume records keep their refusals. The six
real dates keep their verified supplier-basis conclusions.

## C. verifier independence and non-overwriting proofs

`verify()` additionally: re-derives daily coverage itself (missing daily
tree vs a report claiming confirmed no-bar fails), checks the full
manifest (every bound file's hash and root), requires exactly 20 entries
with 20 unique instruments in frozen order before any dict is built,
compares the report's embedded package against the package, and requires
per-instrument reasons. The CLI never overwrites an existing proof — it
refuses with a distinct failure record; failures also persist.

## D. integration and cleanup

Normal merge of master `eb6a832`; accidental debug scripts (.debug_verify,
.fix_delegate, .fix_frozen, .fix_zero) removed by a follow-up commit.
v4 package generated once into a new directory and independently verified
once; v1/v2/v3 untouched. Full pytest/ruff/diff at the final head plus CI.

## E. deliverable

A precise G1/G3/G5 gap list (verified facts vs pending rule decisions vs
missing external evidence, each with its minimal next action) and a draft
limited replay card — no authorization to run it this round.
