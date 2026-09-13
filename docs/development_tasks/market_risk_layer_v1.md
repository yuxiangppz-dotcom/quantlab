# Market risk layer v1 (direct commission, isolated branch)

User-commissioned direct task under AGENTS.md. Codex continues strategy research
in `/home/administrator/projects/quantlab`; this work happens only in the
isolated clone `/home/administrator/projects/quantlab-zcode-risk` on branch
`codex/market-risk-layer-v1`. The old shared ZCode/Codex loop stays paused; no
mailbox, SQLite, claim or reviewer state is touched. The clone was created new
for this task, so no other writer exists by construction.

- Start head (clean, pushed `origin/master`): `72b12772d5283c560ccdb08c921a8fa277291320`
- Scope: new files only — `src/quantlab/research/market_risk.py`,
  `src/quantlab/research/risk_target_adapter.py`, matching tests under
  `tests/research/`, `docs/market_risk_layer_v1.md`, and this card.
  Existing backtest engine, `quantity_kernel`, `quantity_scheduler`,
  portfolio models, fees/corporate actions, factors, daily product and UI stay
  untouched; any shared-file need is stated in the PR for Codex to integrate.
- Budget: zero provider calls, zero canonical writes, zero reruns of sealed
  experiments, zero orders or fill-like records, zero strategy-candidate,
  economic-path or model-fit consumption, no `USER_APPROVED` change.
- This layer decides target exposure caps only. It never certifies inputs,
  reconstructs historical portfolios, executes, or guarantees any drawdown or
  return outcome.

## Exact rule semantics (frozen before implementation)

Common contract:

- `sessions` is a strictly increasing tuple of unique dates with at least three
  entries; duplicates, reverse order or a missing `decision_date` reject
  (ValueError). Duplicate dates inside any input section reject.
- Any observation dated after `decision_date` rejects (future input). The
  sessions calendar may extend beyond `decision_date` (needed only to derive
  the next execution date).
- `next_execution_date` is the first session strictly after `decision_date`;
  when the calendar horizon ends there, the decision remains valid and reports
  `next_execution_date=None` with reason `no_future_session`.
- Windows always mean the last N sessions of the calendar ending at and
  including `decision_date`. Missing values are unknown, never zero-filled.

Rules (ids fixed): `C80`, `C50`, `V`, `M`, `D`, `VM`, `VMD`.

- C80 / C50: constant caps 0.8 / 0.5. Always known.
- V: exactly 60 consecutive sessions of caller-supplied unscaled portfolio
  risk returns (this layer cannot verify the unscaled basis; the source label
  records the claim). Fewer than 60 sessions → unknown `insufficient_warmup`;
  any missing return in the window → unknown `missing_return`; non-finite
  value → unknown `invalid_return`. Sample standard deviation with `ddof=1`,
  annualized by `sqrt(252)`. Cap is `0.8` when the annualized estimate is
  exactly zero, otherwise `min(0.8, 0.15 / estimate)`. De-risked low volatility
  must never come from returns this layer itself scaled.
- M: approved A-share equity proxy is CSI All Share `000985` only. Input
  `index_id` must equal `000985`; any other id rejects (source mismatch).
  Window is exactly 200 consecutive sessions including `decision_date`;
  insufficient warmup → unknown. Every close in the window must be a finite
  positive Decimal; missing → unknown `missing_index_close`, illegal →
  unknown `invalid_index_close`. Mean is `sum/200` (exact Decimal; 1/200
  terminates). Cap 0.8 iff today's close is strictly greater than the mean;
  equality or below → cap 0 (`>=`/strictly-greater fixed here). This rule is
  not applied to gold/bond sub-portfolios and never switches indices.
- D: inputs are the fee-inclusive auditable strategy NAV as of
  `decision_date` plus the persistent `RiskState` (high-water mark,
  coefficient). A NAV flagged with unhandled external flows rejects
  (ValueError); non-positive or non-finite NAV rejects; wrong `as_of` rejects.
  Update order per decision: `hwm_new = max(hwm, nav)`, then
  `drawdown = (hwm_new - nav) / hwm_new` (exact Decimal comparisons).
  Coefficient transitions with at most one recovery step per decision:
  1. `drawdown >= 0.15` → 0.25 (may jump tiers);
  2. else current 0.25 and `drawdown <= 0.12` → 0.5 — the severe-tier
     recovery band, including drawdowns of 10–12%, takes precedence over the
     10% entry rule, otherwise the approved 12% threshold could never fire;
  3. else `drawdown >= 0.10` → `min(current, 0.5)`;
  4. else current 0.5 and `drawdown <= 0.08` → 1.0;
  5. else unchanged. "达到" is `>=`, "以内" is `<=`.
  From 0.25 with `drawdown <= 0.08` the tier moves to 0.5 only — staged
  recovery, never two tiers in one decision. Cap `= 0.8 × coefficient`
  (0.8 / 0.4 / 0.2). The high-water mark never decreases and is never reset by
  empty positions, process restart or a new year; a missing NAV yields unknown
  `nav_unavailable` and returns the state unchanged — no fabricated recovery.
  Initial state: `hwm = first nav`, coefficient 1.
- VM = min(V, M); VMD = min(V, M, D). Any unknown component makes the
  composite unknown with the combined reasons; a D component that evaluated
  still reports its updated state so persistence is not lost when V or M is
  unknown. Missing data is never treated as cap 0 or "risk cleared".

Adapter (`risk_target_adapter.py`): consumes the existing immutable
`TargetPortfolio`. A decision that is unknown or lacks a cap returns a
detectable not-ready result — never an all-cash target and never a silent
copy of the old target. A known cap acts as an upper bound only: lower-risk
targets are never upsized. Cap 0 produces `positions=()` with
`cash_weight=1.0`. Otherwise weights are proportionally scaled down by
`cap / gross` when gross exceeds the cap; ranking, membership and order are
unchanged, no instrument is added, no leverage is introduced, and the
remainder is cash. Negative weights reject. Targets produced here are
intentions: actual limit-down/suspension/T+1 constraints remain the execution
ledger's authority, so realized exposure may stay above the cap; this layer
must be evaluated daily on existing holdings, not only on rebalance days.

## Acceptance

Targeted tests cover: 200/60-session warmup, weekend and session gaps,
threshold equality (M mean equality; D 0.10/0.15/0.12/0.08 boundaries), zero
and illegal volatility, all-cash inputs, unknown-is-not-cash, drawdown tier
down/recovery including staged recovery, high-water persistence across year
change and simulated restart, future-data mutation not affecting past
decisions, weight/cash conservation, no negative weights or leverage, and
unsellable-is-not-de-risked. Full `uv run pytest`, `uv run ruff check .` and
`git diff --check` must pass with pre-existing semantics untouched. The PR
reports start/end SHAs, changed files, check results, known limitations and
interface examples, and claims no drawdown or return guarantee.

## Amendment 2026-09-13 (before any implementation commit)

The first card draft ordered the 10% entry rule before the severe-tier
recovery rule ("deterioration before recovery"). That ordering made the
approved 12% recovery threshold unreachable — a 0.25-tier state could only
recover below a 10% drawdown, so "修复到12%以内可回0.5档" was dead. The list
above now places the severe-tier recovery band (`current 0.25 and drawdown
<= 0.12`, which includes the 10–12% range) ahead of the 10% entry rule. This
amendment was made before the implementation was committed; the failing
boundary tests that exposed it are retained in the test suite.
