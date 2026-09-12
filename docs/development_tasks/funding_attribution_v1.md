# Attribute F4 before treating its association as funding alpha

Starting clean pushed head `c5eeef64a269cf331cabc9dbbd732b4cebfa81ea`, after PR92.
The fixed F1--F4 pilot completed once, with 310,272 code/session rows. Independent
verification rebuilt 2,560 raw windows and all 4,848 daily ICs and 32 summaries.
Report `e9060d47734f52bb4dd11fc90876342f925a54ec51bf1fc92a3e9f3b989becf9`;
proof `6f5911c6a9300b412613171c473d7a0cf9ff11e874b49dfc38d8544a3f4f02e0`.

Observed hypothesis: F1/F2/F3 do not show stable positive associations, while
F4 does. Since F4 contains minus past momentum, the association could be price
reversal rather than incremental funding information. This follow-up answers
that specific question; it does not nominate a new strategy or reverse a funding
feature sign. The 2020--2024 data and these results are already observed.

Before calculations fix one attribution pass using existing saved features only:
on each identical finite F4/F2/momentum20/label cohort compare daily RankIC of
F4, F2 and negative momentum20 (the existing subtractive F4 component). Compute
paired daily IC difference F4 minus the reversal component. Also split each day's
momentum average ranks into five fixed percentile buckets and average the within-
bucket F2/label RankIC weighted by pair counts. Require all five buckets and at
least30 pairs each; ties are not broken by code or outcome. If this condition fails,
conditional IC stays unknown. This is descriptive conditioning, not a causal test,
a new optimized blend, a forecast-model fit or a portfolio return.

Report all five years, 2020--2022, 2023--2024 and whole period. Reuse the predeclared
20-session moving-block interval, 1000 draws, seed20260912. Fixed signs/weights,
no cutoff/holding-period search and no trying multiple bucket counts. One actual
attribution attempt, zero provider calls/economic paths/model fits and no new
funding feature identity (this decomposes already-registered F4). Keep every old
attempt and feature identity in the cumulative program record.

Bind source report/proof and every diagnostic artifact by hash. Only ignored
`data/products/research_program/launch_20260912/funding_attribution` may receive
new output. 10-minute segment, 2 GiB RSS, 128 MiB output, host D reserve8 GiB,
two threads and existing OS locks. Durable intent before processing, no restart
or overwrite after interruption. Independent same-cohort IC/bucket/summary proof.

Add a read-only Chinese "策略研究进度" page to the existing workbench. Show verified
first-batch status, source downloads, yearly factor association and component
comparison; explicitly explain RankIC is not profit, historical PIT is unknown
and no candidate is approved. Missing proof hides the related result; tampering
must not produce a successful display. No run/download-provider/trade button.
Phase statuses distinguish ETF inventory from completed ETF strategy comparison.

Focused formula/paired-population/ties/small-bucket/reader integrity tests, full
pytest/Ruff/diff and Streamlit browser acceptance. Commit/push before the real
attribution pass; PR/CI/merge/exact master handoff and a concrete next ETF-data
card. User-approved v3 allows this bounded failure attribution and workflow
implementation; it does not authorize new portfolio budgets or live authority.
