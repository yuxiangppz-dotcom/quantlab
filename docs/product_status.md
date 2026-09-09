# QuantLab Daily Product Status

This file distinguishes locally exercised product paths from planned work.
It is evidence notes, not a substitute for tests or an investment claim.

## Milestones

| Milestone | Status | Local evidence | Next action |
|---|---|---|---|
| M1 Daily workflow + local UI | Implemented and locally verified | `quantlab doctor`, controlled Provider update, idempotent `quantlab daily`, four-page Streamlit UI, CSV/HTML cache, 926-test regression | Continue to bounded M2 research capability |
| M2 Multi-factor + optional Qlib/ML | Bounded factor batch implemented; Qlib/LightGBM locally blocked | 10-candidate registry, transparent combination, 81 monthly signal dates / 384,319 rows; optional Qlib adapter; fixed LightGBM config | Do not promote before cost/Control evaluation; install missing optional runtimes only with explicit system authority |
| M3 Account input + reference rebalance | Not implemented | Execution contracts exist; UI keeps research targets separate | Add local account snapshot and plan |
| M4 Manual fills + tracking | Not implemented | Transactional in-memory execution ledger exists | Add durable user-import adapter without parallel truth |
| M5 v1 acceptance | Not run | — | End-to-end acceptance after M1–M4 |

## M1 exercised facts

- Canonical inventory was inspected without loading the 60 GB data tree into
  memory. `daily`, `adj_factor`, `daily_basic`, and `index_daily` each had 164
  2026 partitions through 2026-09-04 before the first update attempt.
- A real local snapshot was first generated for 2026-09-04 from 21 required
  sessions in about 2.6 seconds with approximately 391 MiB peak RSS. A
  controlled Provider update then added complete 2026-09-07 through 2026-09-09
  core/index partitions and complete ST/suspension context. The 2026-09-10
  payload was empty before market close, so no 2026-09-10 core partition was
  written and the final exercised snapshot is visibly dated 2026-09-09.
- A second run reused the identical content fingerprint and did not duplicate
  downloads, bookkeeping, or report directories.
- The status remains visibly stale when calendar coverage does not reach the
  requested Shanghai date. The tool does not call the old snapshot “today”.
- The backtest page reads compact manifest-bound daily record files from the
  completed v0.1.3 artifact; it does not load the artifact's 97 MB summary on
  every UI refresh.
- WSL and Windows launch paths both served on `127.0.0.1`; the health endpoint
  returned `ok`, all four pages passed Streamlit AppTest, and Ctrl+C stopped the
  server without a traceback.
- Milestone regression: `926 passed`, `ruff check .` clean, and no diff-check
  errors (the repository retains pre-existing CRLF normalization warnings on
  five untouched historical tests).

Generated product files live under `data/products/` and are intentionally
Git-ignored. They are local cache/output, not Canonical truth.

## M2 exercised facts

- `daily_factor_research_v1/20260910T012031` ran on local QuantLab Canonical
  data from 2020 through signal date 2026-09-02. Runtime was about 147 seconds
  and peak RSS about 2.16 GiB.
- Ten pre-registered candidates were all retained. Four crossed the simple
  validation RankIC thresholds (`momentum_1d`, `intraday_strength`,
  `low_amplitude`, `float_ratio`); the transparent combination had validation
  mean RankIC about 0.031 across 24 monthly observations. These are research
  diagnostics, not strategy profitability or promotion evidence.
- Qlib 0.9.7 is locked as an optional extra and the official
  `StaticDataLoader(DataFrame)` adapter is implemented, but the large optional
  dependency download timed out locally. The run records
  `qlib_available=false`; no fallback is called Qlib.
- LightGBM 4.7.0 is locked in the `research` extra, but its Linux wheel cannot
  load because the host lacks `libgomp.so.1`. The run records the OSError and
  produces no fake predictions or substitute model.
