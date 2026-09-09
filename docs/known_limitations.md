# Known Limitations

## Product

- QuantLab Daily v1 is under staged implementation. M1 supplies local data
  status, ranking, research targets, baseline comparison, and export. Account
  planning and manual tracking arrive in M3/M4.
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

## Data and execution

- Systematic termination-announcement coverage remains blocked by the existing
  `anns_d` permission. Unknown is not safe.
- Absence of an ST/suspension context row is not proof that a stock is tradable.
- Daily close data is not next-session quote, queue, auction, or fill evidence.
- Exact account-specific all-in commission and effective-dated statutory fee
  composition are not yet verified. This blocks execution confirmation, but
  not research ranking or a clearly labelled reference plan.
- Historical, paper, and live execution readiness remain false. There is no
  broker gateway and QuantLab does not submit orders.
- Corporate-action share/cash postings are not yet supported in a real account
  ledger. Adjusted research prices must not be used as a substitute.
