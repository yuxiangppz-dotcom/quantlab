# Research period-contained labels — Issue #35

Direct user commission: continue QuantLab toward a tested, usable local tool.
No other workspace executor is active; reviewer automation remains paused.
Exact clean, pushed starting HEAD: `29c8869d163f0176ce28b3c58c83fb24d8267f3f`.
Branch: `codex/research-period-contained-labels`.

Use one exact global-session label-end filter for factor metrics and ML splits.
Reject invalid/overlapping splits and a configured horizon that disagrees with
the fixed five-session target. Keep unknown endpoints and missing labels excluded;
record distinct exclusion counts, label policy and the calendar hash. Fit feature
medians only on retained discovery rows. Empty training or evaluation is explicit.

Synthetic regressions cover boundaries, sparse stock rows, holidays, missing
labels, invalid splits/horizons, fit/predict inputs, preprocessing, and experiment
metadata. Verify full pytest, Ruff, diff check and the real optional runtimes.
No provider call, canonical write, real-data model training, accounting change,
strategy promotion or order is in scope. The user explicitly authorized installing
project dependencies; libgomp1 was installed from the configured Ubuntu repository.

Commit/push, CI, final HEAD and remaining limitations are recorded in the PR handoff.
