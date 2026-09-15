# Alpha158 replay delivery record

Direct commission, isolated branch codex/alpha158-model-replay-v1. Initial intake base ad5e161, intake b9a653b; continuation card 4eda2eb; user zero-delist and ex-ST amendment 1a32937; initial accounting/acquisition implementation 500b288. The main checkout and scheduled automation were not changed.

User authorized zero delisted valuation and account-level floor for fractional corporate shares. Two fixed variants only: original finite-score Top20, and signal-day ST exclusion before Top20 selection. Same saved models, timing, costs, capital and capacity.

Implemented separate orchestration around the unchanged quantity kernel, dated suspension valuation, corporate receivables/cash/FIFO tax, reviewed issuer-specific distributions, independent accounting verifier and comparison report. All outputs remain outside Git. Raw supplement: exactly88 previously missing ex-ST selected instruments, 88 successful nonempty responses, no retries, no canonical writes. Three issuer source documents supplement recipient/tax/cash interpretation for 000656,002805,688327. No model fits, new strategy optimization or external orders.

Probe failures and repairs are retained: exact-fen reader originally rejected binary float conversion noise in amount/volume; corrected with two-ULP bound for provider parse/unit scaling, never rounding execution quotes. Suspension partitions can contain multiple same-code records; full-day S evidence is checked per code, no unrelated-row rejection. Decimal price bounds are intersected with the integer-fen grid, not mistaken for quotes. Current securities master omitted old code300114; pinned historical code-change evidence resolves it. Dividend None is never globally filled with0: issuer-specific source-bound statements resolve only named events. A base probe finished all895 daily files but failed final input binding because the corporate evidence config was extended while it ran; it is not a valid completed result and remains preserved. Report-script initial syntax/formatting errors were fixed before final execution.

Tests before final immutable execution: full3531 passed,10 skipped,56 existing warnings; ruff and diff checks pass. Final run/proof/report locations and performance are recorded in the local alpha158 comparison artifact. All claims are retrospective simulations rather than broker history or live qualification. Main unresolved financial limits are vendor historical completeness, suspension carry marks, zero delist recovery assumption, fractional floor, daily-bar execution approximation and prior research exposure of the evaluation period.

Do not promote a strategy solely because this engine runs or a report verifies. These checks establish arithmetic consistency within declared assumptions, not profitable alpha or executable live authority.


Final immutable engine: 62cc006, committed and pushed before both final runs.
Both completed 895 days (2023-01-03 to 2026-09-10); independent verifier passed:
original 5,721 executed attempts / 14,365 source files; ex-ST 11,061 / 15,566.
Original cumulative -98.323045%, annualized -68.4114621%, drawdown -98.323045%,
Sharpe -5.648714, ending CNY3,353.91. Ex-ST cumulative -94.720945%, annualized
-56.3569427%, drawdown -94.7810800%, Sharpe -3.968791, ending CNY10,558.11.
Matched SSE price index cumulative +26.2438176%, annualized +6.7896165%,
drawdown -20.4070063%, Sharpe0.501633. All are rf0/252-session metrics.
These fail the user's economic objective; no live promotion is warranted.

Local products under main data/products/model_replay:
alpha158_lgb_original_final_v1, alpha158_lgb_exst_final_v1,
alpha158_comparison_v2 (Chinese Markdown/HTML, plot, JSON and NAV CSV).
Comparison v1 is retained; v2 adds readable fractional-share impact details.
Report-only correction uses canonical instrument_id (not ts_code); no bound
engine or input was modified during final execution. Ex-ST excluded0.4 shares
of688327 on2023-06-13, worth CNY8.54 at that day's raw close, not a terminal
counterfactual profit calculation. Original excluded no fractional shares.
