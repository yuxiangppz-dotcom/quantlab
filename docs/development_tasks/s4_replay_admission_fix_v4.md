# S4 admission fix card v4 (final contract close-out)

Direct commission continuation on `codex/s4-first-replay-admission-v1`.
Reviewed base `039e89712a98c5722bafd78367cb8b9caec8c98d`; mainline
`eb6a832b99adc3149eecbccb8521beb78ecdca43` (already merged at `b8a277e`).
Append-only; Codex tree read-only; automation paused; S5 work on other
branches untouched. Zero downloads/canonical/models/paths/orders.

Closed and not reworked: the merged suite (3299 passed), six numeric
counterexamples, target-date coverage, duplicate-instrument checks, master
integration, debug-script removal, final-head CI.

## A. One contract sweep for the field preflight

Codex reproduced four more silent passes, all rejected only by the
constructor: `additional_fee_rate="-0.1"`,
`rules.effective_from="2021-99-99"`, `fees.effective_from="2021-99-99"`,
`adverse_slippage_rate="1"`. Instead of four more special cases,
`necessary_field_gaps` is reorganized as one systematic sweep over the
kernel contracts (`ResearchSession`, `ResearchFeeScenario`,
`ResearchQuantityRules`, scheduler preconditions): every rate field must be
a finite decimal string in [0,1] with adverse slippage strictly below 1;
`additional_fee_fixed_fen` must be a nonnegative integer when present;
every date-like field is parsed as a real ISO date and the fixed relations
run only on parseable values (from<=through, from<=execution<=through,
prior20_asof<=decision, evidence==execution, next>execution); all other
type/range/unknown rules stay. One pass reports every finding; legal zeros
remain legal known no-trade; None stays unknown. Tests are organized by
contract category: every mutation with an empty gap list must assemble, and
every assembly rejection must be listed as a gap.

## B. Independent verifier must fail on internal semantic errors

Two reproduced false passes are closed. (A) all fields forced admitted with
empty reasons/gaps and a ready verdict, and (B) the manifest shrunk to one
plan file with wrong roots and empty bound_files, both with matching
completed hashes, now fail because the verifier: derives the required
source set from the frozen contract (plan, reconciliation, profiles, the
six intent/result/body triples, a daily and a suspension partition per gap
date) and rejects missing/extra/unmapped manifest entries; checks manifest
roots against the actually passed directories; re-derives per-field
admitted/unknown from the actual context values and required reasons; and
re-derives the overall verdict, stop date and gap summary from the
per-instrument results. The CLI keeps refusing to overwrite an existing
proof. Regressions cover both scenarios with correct completed hashes.

## C. Documentation corrections

G1 rewritten: the sealed ADV20 floor uses the fixed 20-session window and
stays unknown on any missing necessary value; under current scheduler
semantics a necessary prior20 unknown stops the whole session — "two
blocked names keep cash and continue" would be a NEW research rule, listed
as pending decision and not implemented. G3 keeps conclusions scoped to
what was actually verified; local candidate files are mapping tasks and do
not certify full historical eligibility. G5 drops the unsupported
"0.0086% within 200k" tier claim (200k is the expected deployment size,
not a fee tier) and separates declared fees, research scenarios and
unverified facts. The v4 run record now states the true attempt history:
two generation attempts (first output deleted after its verification
crashed on the manifest shape bug fixed in `039e897`; the deleted files are
not recoverable), two verification attempts (first failed, second passed).

## D. Budget and delivery

v5 is generated once into a fresh directory and verified once after the
regressions pass; v1-v4 stay as-is. Full pytest/ruff/diff at the final head
with PR CI verified. No downloads, canonical writes, models, economic
paths, orders or promotions; no historical replay is started.
