# S4 admission fix card v1 (PR #126 review R1-R6)

Direct commission continuation on `codex/s4-first-replay-admission-v1`.
Reviewed base `23e10e1ac72a4f1e28a60dd25e7bf07e94b2f28a`; mainline
`52c4f10` untouched; append-only; Codex directory and review outputs
read-only; automation paused. Zero downloads/canonical/models/paths/orders.

Confirmed progress kept: the seven Fraction volume mappings are correct,
the thirteen others match the sealed integers, ranking and nominal sizes
are untouched (unknown count went 7 -> 0, not 20 -> 0), and the six real
dates do carry local S records with empty suspend_timing and no daily bar,
which supports the supplier-basis full-day suspension reading. These facts
are kept; they are not upgraded into "three independent sources" (all
Tushare-derived).

Frozen sealed identity for this card (programmatic reads now frozen):
plan `6359d58599328340b43a84215ffa2f15725944171edea0b0f2786cf0cadfce92`,
reconciliation
`a9e30b671ace89d59b272d5558295a1b1ee68f75e93c79309bd310d8951c3e10`,
profiles `6e5fbc67e4d8169dfa5797eaca313d525c7106fafb42af282c188abe6845f0de`.

## R1 — corporate flag actually lands in contexts

After verifying the frozen entry contract (decision 2021-12-31 ==
plan signal basis, execution 2022-01-04, initially flat book, buy-only
proposal intents), the derived contexts set
`corporate_actions_processed = true`; fields/open_gaps and the actual
context value must agree. Consumability is proven by type-checked
assembly (`ResearchSession` construction from package contexts), not by
searching reason strings. Sealed contexts stay untouched.

## R2 — suspension evidence read properly

Classification reads `intent.json` (request identity: api/params ts_code
and dates), `response.body` + `result.json` (transport/service status,
rows, status, response sha256 recomputed), the canonical daily partition
and the suspension record including `suspend_timing`. Full-day supplier
suspension requires: verified successful empty response for exactly that
code/date, no local bar, and an S record covering the date with empty
timing. Intraday timing, conflicting bars, missing/invalid responses and
missing-vs-empty fields get distinct classifications; reasons describe
only verified facts; Tushare-derived records are never called independent
sources. `build_input_package` stops hardcoding suspension reasons: each
prior20 gap reason comes from that instrument/date's own determination.
prior20 window rules unchanged.

## R3 — sealed sources actually validated

Reuse `round2_dataset.sealed_read` / `InputBinding`: recompute embedded
fingerprints against the frozen identities above, bind every consumed
file, and re-verify byte-identity after the run. Verify the 256-row
cohort, 20 unique proposals, ranks/slots/nominal quantities/capital,
context securities and decision/execution/following dates; duplicate keys
cannot be silently overwritten; the seven replacements must target
sealed-None slots and reject on pre-existing values, source mismatch or
quantity/date conflicts, keeping old values.

## R4 — full necessary-field preflight + real assembly

Validate every kernel-necessary field with type checks (positive ints,
bools, dates, rule/fee intervals, prior20 == 20 sessions, fee component
completeness) and list all gaps per instrument. Provide a read-only
assembly helper producing the first pending buy orders (signal date
2021-12-31) and an example that passes them explicitly to
`RiskLedgerCheckpoint.start(pending_orders=...)`; assembly executes no
economic path and uses manual evidence in tests.

## R5 — exclusive outputs + honest verifier

`run()` requires a fresh output directory (reject existing dirs and any
path inside the source/canonical trees) and writes started/completed
records. The v1 erroneous deliverables stay; the corrected package is
generated once into a new directory and independently verified once. The
verifier is rebound to the exact package/report hashes and full source
manifest, checks per-instrument contexts and statuses, reads the raw
suspension/response evidence itself, takes explicit directory arguments,
and fails on tampered fields, missing per-instrument justifications,
altered suspension sources or a stale proof paired with a new package.

## R6 — checks at the final head

Full `uv run pytest`, `uv run ruff check .`, `git diff --check` at the
final complete SHA (fixing the 12 lint errors), CI verified on the PR,
and the delivery docs corrected: the G4 claim now matches the context
values, the preflight/proof claims match actual scope, and the fee
wording treats 2022-01-04 as the simulated research date — recording the
declared commission basis and that additional-fee facts stay unknown —
without asking the user to recall a 2022 trade and without new fee
versions.
