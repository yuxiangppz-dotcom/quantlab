# S5-B v1.2 PIT-gated decision assembly

Issue: #143. Starting master: `230d216f697a478e8b63ce3f3fb75673cdc20ae3`.

This task connects the frozen S5-B state kernel and PIT materialization to a
research-only Dynamic-N target. Both sector and stock must be `base_ready` or
`breakout_confirmed`; membership and research eligibility must be positively
verified. Unknown, building and failed inputs are never selected.

Ranking is frozen before outcomes: confirmed before ready, then recovery, close
location, volume confirmation and stable instrument id. The fixed maximum-name
and per-name budgets leave all residual weight in cash, including a valid N=0
portfolio.

No current classification may be backfilled into history. This task cannot
change S5-A/S5-B formulas, access a provider, write Canonical, inspect returns,
fit or tune a model, register/promote a strategy, mutate an account or create
an order. S4 PR #126 remains untouched.
