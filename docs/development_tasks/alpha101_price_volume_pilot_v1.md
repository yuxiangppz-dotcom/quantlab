# Independent S4 information pilot: two fixed Alpha101 formulas

Direct approved v3 commission, after the ETF audit was frozen with its unknowns.
Start clean pushed master `2d1451c557009231669564240cc36082a3b40286`, exact CI34706790256
passed. No concurrent writer/heavy job. Old loop,98fits/60paths remain frozen.

This card authorizes implementation, synthetic checks, commit/push, **one actual
descriptive pilot**, a separate arithmetic review, and a read-only result. No
provider request, canonical/account write, economic portfolio path, model fit,
S4 strategy selection or promotion. It advances an independent price-volume route
without treating ETF event gaps as grounds to abandon strategy research.

## Fixed formulas and input semantics — before inspecting results

Consume **2 of the approved20 Alpha101/191 formula identities** in this batch:
Alpha101#12 and#101 only. Do not run or choose among the remaining18 on this card;
Alpha191 retains a later finite original-source batch. Existing F1–F4 stay4/8.
No sign flips, windows, model fitting or formula combinations after results.

Source: [101 Formulaic Alphas, Appendix A, pages9 and15](https://arxiv.org/pdf/1601.00991).
One direct source PDF GET was attempted with a2MiB cap and no retry. It timed
out; an attempted termination found the process already exited. Preserve both
the initial stop note and its correction. Formula text and version were successfully
read from the official PDF through the web tool; bind that saved observation,
not an invented PDF content hash. No further source downloads on this card.

- #12: `sign(volume[t]-volume[t-1]) * -(close[t]-close[t-1])`.
  It tests joint volume-change and short price-reversal information. Raw prices
  are CNY, volume has the fixed existing canonical unit. Positive unit scaling
  does not change volume-difference sign. Reject the pair when either bar is
  invalid/missing or its recorded adjustment factor changes; do not let a split/
  dividend price step manufacture this raw-price signal. This explicit action
  mask is a local adaptation, not an unqualified exact reproduction of the paper.
- #101: `(close-open)/(high-low+0.001)`, raw CNY prices, including the source's
  literal0.001CNY denominator constant. This is intraday directional/range
  information and is kept with its original sign. It is not a separately tested
  bearish variant. Flat valid bars produce0, not fabricated trading capacity.

Reuse ONLY the saved310272-row, fixed256-stock2020–2024 grid from the sealed
funding pilot. Reindex against its exact common calendar and codes; duplicates,
missing rows or identity changes fail rather than collapsing time. A missing bar
stays unknown. #12's first grid session is unknown; do not fabricate the preceding
bar or recompute the old funding pilot to fill that one-day warmup.
Use the already-saved year-contained t+1-close to t+6-close diagnostic label and
20-day momentum. No new label, return horizon, universe or ETF data is computed.
The label is an adjusted research-price association, not executable net return.

Each new formula's daily RankIC is paired with **negative20-day momentum on the
identical formula/label/momentum-complete rows**; minimum30 pairs, average ranks,
constant vectors/empty groups stay unknown. Report paired IC difference, formula
correlation with reversal, size and turnover, and inter-formula redundancy on
signal-only complete rows. These are diagnostics, not economic controls or new
funding factors. No financial/PIT certification is inherited from the funding file.

Freeze annual2020–2024,2020–2022,2023–2024 and whole2020–2024 summaries. Moving
calendar-block interval:20sessions,1000draws,seed20260912, same existing estimator.
Keep every outcome; intervals are descriptive and not multiple-testing corrected.
No Sharpe/CAGR/drawdown, simulated fill, stock list or trade recommendation.

## Resource, attempt and evidence gate

One actual attempt under the existing shared heavy-job OS lock and a unique output
lock.512MiB new output,2GiBRSS,600seconds,8GiB physicalD reserve; no nested heavy
worker. Write intent before any new formula; a failed/aborted start consumes the
attempt, never reset or overwrite. Require clean pushed source and unchanged
parent report/proof/input hashes before and after computation. Store exact code
binding, formula identities, masks, daily metrics, summaries and resource usage.

The existing retrospective universe/PIT/label and execution limitations remain;
even a favorable result cannot admit a historical candidate. The useful decision
is whether either fixed formula merits a later explicitly registered strategy or
information-enhancement comparison. A failure/negative result is retained rather
than flipped into a new candidate. F8,ETF returns and S2/S3 audits remain separate.

Verify literal formula arithmetic independently, future-row perturbations,
adjustment-step/missing-bar handling, calendar gaps, duplicate identity, constant
signals, common-cohort comparison, attempt reuse rejection and source tampering.
Run full pytest/Ruff/diff, commit/push before actual run, review saved arithmetic,
then complete PR/exact CI/merge/master CI. Report all counts/limitations honestly.

Implementation verification before the actual attempt:19 focused new tests
(30 including existing progress tests), full2093 passed/10 optional-runtime
skipped/56 existing NumPy warnings. Ruff and diff checks passed. Self-review
made both paired IC series unknown when either is constant, so whole-period
means cannot accidentally use different valid-date sets. Initial UI line-length
lint was fixed; the final suite was rerun after the added evidence-display tests.
