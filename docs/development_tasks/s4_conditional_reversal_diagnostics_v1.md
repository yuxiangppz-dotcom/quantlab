# S4 next finite task: three fixed conditional-reversal diagnostics

Direct approved v3 commission. Implementation baseline is master
`88e45a5d964d523a3508f21c01e6a0da56fb39bf`. The final handoff after this
document-only merge supplies the exact clean pushed starting HEAD; bind it before
implementation, and require its exact CI to pass.
Confirm no concurrent writer/heavy worker. Shared mailbox stays paused.
This card authorizes implementation, tests, commit/push before one actual
signal-diagnostic segment, independent recomputation, findings and proof-gated UI.
No provider request, canonical write, new label horizon, model, economic path,
order, or strategy promotion is authorized by this card. Do not rerun the
completed funding, ETF, Alpha101 or 43-request context batches.

## Hypothesis and scope fixed before new outcomes

Evaluate the approved S4 family's three definitions: a three-session relative
fall, the same fall conditional on medium-term positive trend, and the same fall
conditional on an existing funding signal crossing into positive territory.
These are S4-A/B/C's signal diagnostics, not three new strategy families or
validated tradable portfolios. They use three of the existing 15 rule identities
on actual start; do not create S4-D or change conditions after viewing outcomes.
Existing 20-session reversal is a fixed information reference, not a new candidate.
The prior #12 or F4 results do not justify choosing a winning sign/window here.

Reuse exactly the existing sorted 256-code 2019 cohort and 2020–2024 calendar.
Input: funding_diagnostics/features_and_labels.parquet, SHA256
`52d83a4afe0f34f0a9d1ba211fa6da2250273cb5dd7a2f9decab483cffe3721f`,
parent report `e9060d47734f52bb4dd11fc90876342f925a54ec51bf1fc92a3e9f3b989becf9`
and independent proof, plus sealed funding_inputs.json and selection.json.
The parent's 61 earlier sessions from 2019-10-08 through 2019-12-31 may supply
price warmup only: read only daily and adj_factor files already hash-bound in that
manifest, with the same 256 codes, positive finite OHLCV, valid bar ranges and
positive finite adjustment factor. Reindex every code/session; never compress a
gap, forward-fill or select today's surviving stocks. Bind all exact input hashes
and bytes in config before the run. No new download or flow recalculation.

Define adjusted research close P=raw_close*adj_factor on valid bars. An n-session
change is P(t)/P(t-n)-1 only when all n+1 calendar observations are valid. This is
an adjusted research feature, not an executable price or a cash/dividend ledger.
S4-A score is median(valid three-session changes on that date) minus the stock's
three-session change. This cross-sectional centering does not change reversal
ranking. Missing price windows remain unknown, not zero signals.
S4-B retains that same score only when the stock's 60-session change is strictly
positive; zero/negative fails the condition, missing stays explicitly unknown.
S4-C retains A's score only when existing F1(t)>0 and F1(t-1)<=0, with both finite
on adjacent exchange sessions. F1 is the sealed five-session large-trade imbalance,
not identified institutional ownership. The first saved 2020 F1 lacks a saved prior
value and is unknown for C; do not recompute or backfill an unseen prior F1.
Do not backfill filtered candidates or redistribute their weight in this diagnostic.

Use only the existing year-contained label_5 and its saved t+1/t+6 dates. Labels
are research price comparisons, not achieved five-day exits. No overlapping-window
assumption of independent trades and no compounding, weights, fees or NAV here.

## Fixed outputs and interpretation

Preserve all 310272 code/date positions with A score, three-state B/C conditions
(pass/fail/unknown), retained scores and feature-valid masks. Daily report counts
before and after each condition, label-complete counts and condition unknowns.
For each variant compute Spearman correlation with label_5 using average tied
ranks, minimum30 complete pairs. Also compare to negative saved momentum20 on
exactly the same finite rows; if either correlation is undefined, both paired
statistics remain undefined. No low-sample replacement or lower threshold for C.

Report five annual periods, 2020–2022, 2023–2024 and 2020–2024, keeping all dates
and undefined days. Mean daily IC and paired difference receive the same fixed
20-session block/1000 resamples/seed20260912 as earlier diagnostics. Report
signal-only size, turnover and reversal correlations. B/C versus A on different
populations is descriptive conditioning, not causal incremental alpha; low coverage
is itself a limitation, not an excuse to loosen a condition after seeing results.
No quantile-return, hypothetical portfolio, annualized return, drawdown or retained
strategy claim is allowed from this signal-only task. Apply v3 economic retention
rules only after separately authorized admissible net-cost paths exist.

One actual segment including all three variants, max600seconds/2GiBRSS/512MiB new
output/8GiB physicalD reserve; one heavy-job OS lock. Failed or interrupted starts
consume the segment; preserve receipts and do not reset. No new Alpha101/191 formula
or funding identity: cumulative remains2/20 and4/8 respectively, economic paths0/80,
first fits0/6. S4 future A/B/C baseline/stress pairs would consume6 of the22 remaining
initial-strategy path reservations, but none is run under this card.

## Verification and next decision

Synthetic tests cover future-price mutations not changing prior signals, calendar
gaps, code boundaries, all61 warmup observations, strict threshold equality,
unknown versus failed conditions, unchanged F1 source values, sparse/constant
paired ranks and intact year-contained labels. Full pytest, Ruff and diff checks;
self-review then commit/push before the actual run. Separate scalar arithmetic and
tie-rank checker must reconcile every output row and daily summary without rerunning
the actual writer. Preserve findings, source hashes, attempts and honest limitations;
PR CI/merge and exact-master CI close the part before the next task.

After this signal task, prioritize the minimum execution/input feasibility for
S4-A's two fixed cost scenarios: historical price limits, suspension/risk context,
corporate actions, 200k order sizes, minimum commission, effective-dated stamp tax,
prior20-day amount participation and unexitable holdings. Time-box this prerequisite
review and freeze unresolved evidence. The old Issue90 input audit is separate and
must not be reset or used to block every independent route. No historical risk/PIT
unknown may silently become tradable; if a net-cost replay is infeasible, say so and
prepare a specifically bounded prospective or independent-family task. Do not make
more peripheral UI/account modules a prerequisite for testing usable price signals.
