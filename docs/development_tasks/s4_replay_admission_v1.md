# S4 first-replay local admission & preflight card v1

Direct commission on branch `codex/s4-first-replay-admission-v1` in the
isolated clone. Start head (clean pushed master, PR #124/#125 merged):
`52c4f10ab4428edeebcb9d2742a90ac86c12b94d`. Codex directory is read-only;
automation paused; old mailbox/SQLite untouched.

Scope: zero provider calls, downloads, canonical writes, model fits,
economic paths, orders or promotions. New verification programs write only
to this workspace's new output directory. Sealed artifacts are never
modified or recomputed; fingerprints are read programmatically, never
copied from memory.

## Frozen first-day semantics (unchanged from the sealed plan)

Decision 2021-12-31, execution 2022-01-04, following 2022-01-05; initial
flat, 200,000 CNY; sealed 256-cohort ranking and the 20 proposals with
their nominal quantities (decision-day raw close + closing-LIMIT grid);
80% gross, 8,000 CNY per slot; no substitution, no budget redistribution,
no re-sizing from execution-day data; baseline fee/slip/capacity profile
kept; no new cost or risk versions.

## Work items

1. G2 admission: consume `s4_entry_raw_precision/reconciliation.json`
   `execution_volume_precision` rows; per row verify instrument ∈ the 20
   proposals, trade_date == 2022-01-04, `source_status == nonempty`, and
   the unit mapping numerically (raw text vol in lots x 100 == integer
   shares; amount text in kilo-CNY x 1000 -> fen x100) before mapping
   `session_volume_shares` into NEW derived execution contexts. Sealed
   contexts and `automatically_admitted=false` records are preserved; this
   is a numeric admission only and upgrades nothing else.
2. G1 bounded check: for the six empty prior20 dates, hash the sealed raw
   attempt files, and consult the read-only canonical daily partition and
   lifecycle suspension context for that code-date. Classify each date
   (supplier-empty-confirmed / local-bar-present / suspension-record-found /
   undetermined). Empty returns are never interpreted as zero volume,
   suspension or non-trading; prior20 windows stay unknown; the frozen
   missing rule is not changed — only proposals may be listed.
3. G3: per-proposal board from code prefix, per-instrument quantity rule
   from the sealed context, applicability by date; identity/signal
   eligibility and market-open stay explicit unknowns (absence of records
   is not proof of tradability).
4. G4: scope corporate actions to the first day: initial flat book,
   buy-only intents — no held lot crosses an ex-date on the entry day, so
   entry-day contexts may carry `corporate_actions_processed=true` with a
   per-instrument written justification; this covers the entry day only
   and never future holding periods.
5. G5: fee boundary statement — user-declared commission 0.0086% (min 5
   CNY) and date-dependent sell stamp are known; additional fee rate/fixed
   stay explicit unknowns (never zero-filled, never double-counted). The
   minimal user question is listed once, with the exact fields it blocks.

## Deliverables

`src/quantlab/research/s4_replay_admission.py` + CLI (`python -m
quantlab.research.s4_replay_admission --source-dir ... --output-dir ...`)
producing, once, into the new output directory: a source manifest with
SHA-256 hashes and sealed-fingerprint cross-checks; the derived input
package (20 proposals, nominal quantities, proposed execution contexts
with per-field source/admission status); a preflight report with every
necessary gap per instrument (not first-error-only), the overall verdict
and the precise stop date; and consumption notes for
`RiskLedgerCheckpoint` / `LedgerSessionEvidence`. First pending intents
keep the 2021-12-31 signal date. No simulated fills, returns, CAGR or
drawdowns; full admission is labelled only as "inputs ready to request the
first replay run" — economic paths stay 0 and the run itself needs a new
limited card.

## Tests and verification

Targeted tests: exact-volume mapping accepts the seven and rejects
instrument/date/unit/source mismatches; execution-day data cannot change
ranking, slots or nominal quantities; missing/conflicting evidence stays
unknown (no silent upgrades); per-instrument rules checked by code and
date; preflight performs no provider calls and writes nothing outside the
output directory; sealed input hashes identical before/after. One
generation, then one independent verification program (Fraction
arithmetic + source re-mapping, not the production functions) re-checks
the package. Manifest/output locations and resource bounds frozen here:
single-threaded local files only, no network, output < 50 MB.
