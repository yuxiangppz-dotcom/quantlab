# Issue #62: native Alpha158 mapping and bounded parity audit

Direct single-agent recurring commission. Start at clean pushed
`1dc22d606617c0caa59db02dac94dfe1b6e8bdc4`, master CI 34551655992 passed;
no old executor/reviewer or heavy research process was found. Host D has over
100 GiB free and WSL over 14 GiB available RAM at preflight. First batch is capped
at 2 GiB generated data, at most two computation threads and zero research fits.

Before real factor outcomes, `config/qlib_alpha158_audit_v1.json` pins Qlib 0.9.7,
five relevant upstream Python source hashes, all 158 default expressions/names,
field dependencies and native lookback windows (0–60 sessions, no future use).
All installed Qlib Python/compiled files are additionally bound at execution.
Fixed codes: 000001.SZ, 600000.SH, 688001.SH, 300114.SZ, 302132.SZ. Fixed target
interval: 2024-07-01 through 2025-06-30, with 120 earlier market sessions.
This is a transport/mapping/causality audit, not a representative alpha sample.

Use native Qlib expressions over an isolated float32 binary cache. Compare all
158 expressions with the same pinned upstream engine reading an independent
in-memory field provider. This checks data transport and mapping; it is NOT an
independent implementation of 158 mathematical formulas or proof of alpha.
Independent analytic fixtures check OHLC ratios, price/volume scaling, synthetic
split consistency, flat bars, zero/missing inputs, calendar gaps and prefix/future
invariance. Tests never write canonical market observations or fit real models.

Explicit mapping: adjusted OHLC = raw OHLC × same-date adj_factor; adjusted
volume = raw shares / adj_factor; VWAP = amount CNY / raw shares × adj_factor.
No rebasing by the final date, predecessor/successor stitching, current constituent
filter, zero-filled missing input, label column, or forward-looking reference.
The amount/volume VWAP is a derived ratio, not an independently observed quote.
A ratio outside raw low/high is retained in input evidence but its VWAP feature
is masked; no unverified rounding tolerance is introduced.

Raw native outputs are kept separately. A usable feature requires every declared
dependency over its full calendar lookback plus current session, and a finite
native result. Nonpositive volume is preserved as raw evidence but missing for
volume-dependent features. This is deliberately stricter than Qlib's native
partial rolling windows/Boolean handling; it is an eligibility policy, not a
claim that missing-data semantics equal Qlib's default. Constant inputs may still
produce undefined correlation/regression factors; these remain unknown.

Prior smoke inspection used only 90 synthetic sessions to confirm both native
providers can execute 158 expressions. It produced no real factor outcomes,
model fits or selection decisions. The real audit is exclusive and source-bound,
with any failure retained. Implementation is tested, committed and pushed before
execution; Chinese actual findings/UI form a later reviewable part. Complete full
pytest/Ruff/diff, optional Qlib/LightGBM and native fixture checks, browser review,
PR, green CI, merge, master CI and a next task. Keep prior portfolio blockers.

No data-provider API calls, canonical writes, account/orders/fills, forward
registration, model search, IC optimization, return/drawdown claims, C-drive
cleanup, old agent-loop changes, purchases or automatic strategy promotion.

Implementation validation before the real audit: 1,420 default tests passed,
3 optional checks skipped by default; all 3 explicitly enabled runtime tests
passed (including the native synthetic Alpha158 fixtures). Ruff and diff checks
passed. Self-review added fail-closed report checks and a two-thread runtime gate.

Actual audit at committed/pushed f4bbadc92272588b538fce912e12d4316f547f3d completed
once in 10.901471 seconds: 362 sessions with warm-up, 1,210 target identities,
968 active identities, all 158 expressions matched with zero differences,
149,404 usable target factor cells and 908 fully usable identities. The exclusive
output including report is 2,790,578 bytes. No failed real attempt or research
fit occurred. Raw/native/usable outputs, source/code/runtime hashes, simulations
and code exclusions remain separately inspectable. Independent verification
checked all 1,032 inputs and 141 output artifacts before presentation edits,
plus raw unit/adjustment calculations and code-switch exclusion boundaries.
See docs/alpha158_findings_zh.md for report and verification fingerprints.

Final local checks: 1,420 passed / 3 default skips; all 3 explicitly enabled
runtime checks passed; Ruff and diff checks passed. Real headless Edge acceptance
verified the Chinese factor table, 968 active rows, code boundaries, limitations,
and both report/Chinese downloads with zero browser or Streamlit exceptions;
the screenshot was visually inspected. Self-review also prevents a resealed
report from claiming more usable observations than active code identities.
Only precommit formatting/style issues were corrected; the real batch required
no retry or scope change. The shared native engine is explicitly disclosed and
not described as an independent implementation of 158 formulas. Freeze these
artifacts; the next task should stage a resource-bounded historical universe
before a separately predeclared rolling comparison. No alpha is promoted.
