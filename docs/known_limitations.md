# Known Limitations

## Product

- QuantLab Daily v1 covers daily research, versioned user configurations,
  account reference planning, manual-fill tracking, export and local acceptance.
- A cached report is viewable offline, but its exact `effective_as_of` remains
  prominent. Offline availability is not evidence of fresh market data.
- The local UI binds only to `127.0.0.1`; it has no authentication and must not
  be exposed to a public interface.

## Research

- `momentum_20d_reversal_example` is a test-observed research example. The
  historical formal result has negative active return versus the same-universe
  equal-weight control; it is not a profitability or investment claim.
- The 5/20-session horizons are research labels and features, not validated
  holding periods. No 20-day forced exit or “7–15 day average holding” claim is
  implemented.
- The formal settlement paths use explicit recovery assumptions. They do not
  prove actual delisting proceeds or fills.
- The bounded factor batch, daily/weekly Discovery cadence audit, and 2020–2024
  Portfolio Translation Audit are retrospective/test-observed. Positive
  historical active CAGR is not fresh OOS and no candidate is auto-promoted.
- Qlib 0.9.7 and the StaticDataLoader adapter run locally. Only KMID and KLEN
  have exact Alpha158 mappings; the style set is not a complete Alpha158
  implementation. Qlib does not own Canonical data or accounting.
- The LightGBM wheel is present in the optional research environment but the
  WSL host lacks `libgomp.so.1`. No LightGBM model was trained and no fallback
  model is labelled as LightGBM.
- Forward Shadow starts at 2026-09-09, so it has no matured 20-session evidence
  yet. Original predictions are immutable; later diagnostics cannot turn this
  short history into historical OOS evidence.

## Data and execution

- Systematic termination-announcement coverage remains blocked by the existing
  `anns_d` permission. Unknown is not safe.
- Absence of an ST/suspension context row is not proof that a stock is tradable.
- Daily close data is not next-session quote, queue, auction, or fill evidence.
- Exact account-specific all-in commission and effective-dated statutory fee
  composition are not yet verified. The reference plan estimates only the
  user-reported commission (0.86/10000 up to CNY 500k, 0.80/10000 above it,
  CNY 5 minimum). This blocks execution confirmation, not research ranking or
  the clearly labelled reference plan.
- 2026 quantity-grid rules are engineering assumptions in the reference plan;
  all legs remain pending next-session rule and market-status review.
- Historical, paper, and live execution readiness remain false. There is no
  broker gateway and QuantLab does not submit orders.
- Corporate-action share/cash postings are not yet supported in a real account
  ledger. Adjusted research prices must not be used as a substitute.
- `fina_indicator_vip` exposes announcement date and update flag but no revision
  timestamp. Stored rows are prospective from first local observation
  (2026-09-10), not valid for historical PIT backfill; no fundamental alpha was
  admitted in v1.1.
- `dividend` is context/warning only. It does not post cash or shares, and
  adjusted-price research returns must not receive a second dividend adjustment.
- Manual tracking does not yet model deposits or withdrawals. Its displayed
  return is explicitly a raw-close reference mark from the opening snapshot,
  not a broker-verified performance record. It remains unavailable when prices
  predate a fill or a held instrument cannot be valued.
- Opening aggregate holdings are adapted into execution-ledger availability
  lots: imported sellable shares are immediately available; imported
  unavailable shares become available on the next known session. This is an
  accounting adapter, not an assertion of their historical acquisition date.
- `demo_simulation` is isolated from manual-fill import. QuantLab v1 does not
  infer simulated fills from daily bars; no queue, slippage, or fill claim is
  made.
