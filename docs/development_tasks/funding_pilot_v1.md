# F1--F4 funding diagnostic pilot, approved v3 program

Start at clean pushed `34d87c3b2ae578e5096e190b9ac7025f55980c3a`, following PR91.
The 259-request intake completed once: 256 funding histories with 321,382 rows,
677 terminated and 2,213 live exchange-fund metadata rows, one successful empty
issuing-status response. Independent raw verification found zero duplicate keys.
Its report fingerprint is
`a97569d2e90dfa268b8d697dfe8b0ad34cff7b781c67ea3ca46b15ecb189850f` and proof is
`771bb0f2d437928a8c0581448bc9ad7ffca8a6b249851df9f643ce144207383a`.
Those observations do not certify historical publication or revision versions.

This task authorizes one local diagnostic processing attempt, four of the eight
v3 funding feature identities, zero provider calls, zero economic paths and zero
model fits. Do not select a strategy or reverse a feature sign from these results.
Keep the remaining four funding features, 15 strategy candidates, 80 economic
paths and six first-round fits unconsumed. Pure software tests and an independent
verification of the same outputs are not additional experiments.

Inputs: fixed 256-code cohort from the 2019-12-31 observed daily population,
all 1,273 market sessions from 2019-10-08 to 2024-12-31, saved raw daily,
adjustment and daily-basic partitions, original funding bodies/receipts. Verify
the complete source manifest before/after processing. Do not remove later
delisted securities or infer listing/eligibility from current status. Historical
universe completeness remains unverified; no general all-market claim.

Predeclared formulas, with large/extra-large active flows converted from CNY
10,000 units to CNY and canonical traded amount already in CNY:

- Let N = (buy_lg_amount + buy_elg_amount - sell_lg_amount - sell_elg_amount)*10000.
- F1 = sum(N,5)/sum(traded_amount,5).
- F2 = sum(N,20)/sum(traded_amount,20).
- F3 = mean(N>0,20), missing observations remain missing, not nonpositive.
- F4 = cross-sectional percentile rank(F2) minus percentile rank(20-session
  adjusted-close momentum). Average ties, same-day fixed cohort; no winsorization
  or fit. These are mechanism hypotheses, not proof of institutional activity.

Reindex onto every market session for every code. Missing/invalid bar or funding
value breaks the applicable lookback; do not bridge suspension gaps or treat
missing flow as zero. Amount/volume and OHLC must be positive/consistent; per-
category buys/sells must be finite nonnegative numerics. Keep the original rows
and separate flags; total net_mf_amount remains a distinct unused field.
20-session momentum requires 21 valid adjusted closes. Market cap and turnover
are diagnostics only and must not determine which labels/features are retained.

Signal dates 2020--2024 only. The one label is adjusted close at t+6 divided by
adjusted close at t+1 minus one, on exact market sessions; require all six held-
interval close observations and endpoints. This is a hypothetical five-session
research label, not executable prices/fills, cash return or known tradability.
Require both endpoints in the signal's calendar year for every annual/aggregate
diagnostic. Missing labels remain visible and are never filled or used to filter
feature construction. Raw observations collected in 2026 have unknown historical
release/revision provenance: label this entire pilot retrospective exploratory,
ineligible for historical candidate retention. No inferred next-day PIT guarantee.

Metrics: daily Spearman RankIC via Pearson correlation of average ranks, at least
30 finite pairs and nonconstant vectors; yearly and 2020--2022/2023--2024 summaries
in fixed F1--F4 order; feature pair correlations and correlations with log market
cap and turnover. Report missingness, pair counts and valid dates. No portfolio
annualization, fees, top-N returns or sign/period optimization. Describe uncertainty
with a fixed 20-session moving-block bootstrap, 1,000 resamples, seed 20260912,
without calling overlapping daily observations independent or claiming untouched
holdout/significance. Undefined correlations/intervals stay null.

Limits: one actual attempt, 20-minute processing segment, 4 GiB RSS, 512 MiB new
ignored output, host D reserve 8 GiB, two compute/Arrow threads. Use existing heavy
and output locks; durable intent before calculation. An interrupted/failed attempt
is not restarted under a new ID. Freeze input manifest and commit/push tested code
before actual computation. Completed outputs are reused by verified readers.

Validation: exact units/formulas, no future feature leakage, calendar gaps, missing
flow, invalid/duplicate inputs, period-contained label timing, F4 cross-sectional
ties, constant/small-sample IC, source tampering, one-attempt guard. Full pytest,
Ruff, diff; independently recompute the calendar/units/formulas on a fixed sample
and all daily correlation summaries from saved rows without production mappers.
Return an honest Chinese diagnostic report with downloadable evidence; update
PR/CI/merge/master handoff and cumulative program progress. No canonical/account
write, new provider request, model fit, economic replay or strategy promotion.
