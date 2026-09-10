# Issue #58 — execute the predeclared second diagnostic round

Starting clean/pushed HEAD: `8962d5e1524284b23c367a5b4130403193b8085b`.
The direct single-agent commission applies; no old executor/reviewer was writing the workspace.
Branch: `codex/research-round2-diagnostics`; PR: #59.

Implementation commits, both pushed before reading diagnostic outcomes:

- `fb2d3122675954428fe25a877188306c1c40fa3f`: frozen config and causal features; 1,390 base tests passed.
- `1e116ebdec50870cc728e6bf280f220274449622`: source staging, six-fit runner, Chinese review and usage;
  1,402 base tests passed. This is the exact real-run implementation HEAD.

The subsequent report/UI commit and final master HEAD are recorded in PR #59 and the local final
handoff receipt `data/products/delivery/20260911/round2_handoff.json`. No commit is amended or forced.

## Actual run and findings

The frozen run completed at 2026-09-11 03:02:22.752284 Asia/Shanghai, after starting at
02:57:33.339316. Runtime was 289.413 seconds. All 21 comparisons and all six real fits completed;
no retries, hidden failures, early stopping, grids, direction flips or additional model fits.

Stage fingerprint: `99969fc8bf35c7a03719cddbf8c8219eebd4310489ccc9416ac4014bd10994bf`.
Result fingerprint: `30a2b139368e7f5d5acf9df53379d4596059e9f91cc5e2f41d19aed3a3c12aa0`.
Output root: `data/products/research_round2/research_round2_20260911`.
Runtime: Python 3.12.14, pandas 3.0.5, NumPy 2.5.2, PyArrow 25.0.1, LightGBM 4.7.0, two model threads.
Outputs total approximately 2.83 GB, on the D-backed WSL filesystem; no output enters Git.

Read window: 1,743 sessions from 2019-07-09 through 2026-09-10. The source identity accounting
is 8,426,396 daily rows: 8,154,548 retained in historical V1 scope (including warmup), 270,393 outside
that scope, and 1,455 outside known code/lifecycle dates. Every raw daily identity remains staged
with an exclusion reason. The target research interval has 7,713,423 rows; 6,963,522 have all
18 required features. Labels are subsequently required to be finite and contained within their period.

The 13 missing SH/SZ basic joins and 15 inconsistent cap rows retain their identities and raw
values. Cap-dependent features are missing on those 28 rows. No clipping, fabricated correction,
current-constituent substitution or missing-label fill is used. STAR50's publication gate excludes
505,161 stock observations before 2020-07-23; the common diagnostic training cohort therefore
does not cover 2020's first half. Source/history and future-label-availability conditioning are
material limitations, not a claim of tradability.

The full 21-row results and interpretation are in [中文结果解读](../research_round2_findings_zh.md).
For example, 20-session baseline model validation/observed IC is 0.130109 / 0.138407, compared with
0.121418 / 0.053679 for the augmented model. All three augmented models have higher training IC
but lower observed-period IC than their baseline counterparts. This does not establish a cause
or executable profitability. No model is promoted. Momentum and MACD's predeclared directions
remain disclosed despite negative average validation/observed IC; reversal was predeclared.

## Implementation and self-review

Changed files:

- `config/research_round2_v1.json`: exact pinned contract and 21/six budgets.
- `research/round2_features.py`: 60-consecutive-session MACD, raw wick balance, complete-window
  SSE/STAR50 features, invalid-cap masks and delayed whole-universe combination normalization.
- `research/round2_dataset.py`: bounded daily reads, frozen price/PIT helpers and exact-session
  returns, year/bucket staging, source identity retention, byte hashes and source drift checks.
- `research/round2_diagnostics.py`: contained common cohorts, training-only medians, fixed fits,
  exclusive round/candidate intents, failure receipts, saved models/predictions and daily/annual IC.
- `ui/round2_review.py` and `ui/app.py`: read-only Chinese comparison, annual/coverage tables,
  compact validation-first columns and downloads, separate from the old frozen backtest view.
- Three `test_round2_*.py` files: 24 new tests. Two user guides and this task report document use.

Self-review found and resolved the following before delivery:

- Per-bucket cross-sectional normalization would be wrong; combination normalization explicitly
  runs across the entire PIT universe after continuous per-security features, before label filtering.
- Both known inactive old codes and successor backcasts must be excluded by existing exact bounds;
  unknown identifiers still fail. The real old/new-code boundary was independently checked.
- Main review columns were too wide and JSON key ordering put annual rows out of order; the final
  view uses compact validation/observed/training columns and explicit chronological annual order.
- The browser harness initially clicked an editable combobox without opening its option list;
  using its keyboard opening action and restarting the owned UI after edits resolved the harness.

No material research-scope deviations occurred. The common complete-feature cohort, raw target,
fixed float32 feature matrices and all parameters were committed before outcome inspection.
Whole-round and per-fit exclusivity deliberately refuse unattended retraining after interruption;
partial artifacts and intent receipts remain evidence, requiring a separately scoped recovery.

## Verification and acceptance

Final checks: `uv run pytest` (1,402 passed, two optional tests skipped), `uv run ruff check .`,
`git diff --check`; both explicit optional LightGBM/Qlib runtime checks passed. The implementation
PR CI run 34517292701 passed both jobs; final PR and post-merge CI are recorded in the handoff receipt.

Independent real-data verification, without fitting:

- All 5,524 input/code files matched their bound hashes at completion and independent recheck;
  all 263 staged artifacts and 65 result artifacts were verified.
- All 63 period summaries equal their saved daily IC series. All nine baseline/augmented
  horizon-period cohorts match row-for-row, including labels and exact endpoint dates.
- Six saved models reproduced 36 sampled predictions across training, validation and observed
  periods; all preprocessing receipts end their training labels within 2022.
- Frozen reference algorithm parity on 2020-09-22, 2025-02-14 and 2025-02-17: 14,240 rows and all
  three historical/three future returns match, including old/new code inclusion and missingness.

Independent receipts: `actual_reference_spotcheck.json`, `actual_final_verification.json` in the
output root. Browser acceptance: `data/runtime/ui/browser_round2.json`, screenshot `final-round2.png`.
The real local Edge session changed 10/20/5 horizons, selected the augmented annual view, expanded
coverage and downloaded the 6,194-byte Chinese interpretation; no page errors or Streamlit exceptions.
The screenshot was visually checked after compacting the table and correcting annual order.

The UI is available at http://127.0.0.1:8501 via the existing QuantLab desktop shortcut.
See [第二轮使用说明](../research_round2_usage_zh.md). Research source hashes describe the run's
committed snapshot; later documentation/UI presentation changes do not rewrite that evidence.

## Freeze and next step

Freeze this completed round and preserve every result. Do not rerun for a better result. A bounded
next task should translate the simple baselines and fixed models into existing portfolio feasibility
checks with explicit capital/cost scenarios, next-session execution, minimum one-session holding,
5/10/20-session intended exits, failed exits, turnover and drawdown accounting. It must declare
remaining unknown evidence and distinguish scenario results from executable performance.

No provider calls, canonical writes, broker orders, synthetic actual fills, account changes,
backfilled forward registrations, strategy promotion or changes to frozen backtest/ledger/execution
semantics occurred in this Issue. Historical revisions, full corporate actions, tradability and complete
cost coverage remain unverified. Net returns and the user's 20% drawdown target remain unassessed.
