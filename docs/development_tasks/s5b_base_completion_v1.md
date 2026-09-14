# S5-B v1 base-completion kernel

Issue: #139. Parent program: #127. Starting master: `7bc9421ae0b5c8646c22dcea5fb3e2bea408a3b6`.

## Frozen hypothesis

A recent 60-session low is not independently bearish. S5-B separates shallow,
recoverable undercuts from structural failure by observing undercut depth,
10-session decline speed/drawdown, volatility expansion, 10/20-session new-low
frequency, reclaim speed, recovery and short-term price structure.

The five states are `unknown`, `base_building`, `base_ready`,
`breakout_confirmed`, and `failed`. Price and volume are both required for
breakout confirmation. The kernel makes no claim about investor identity or
intent.

## Frozen v1 thresholds

- prior 120-session drawdown: at least 20%;
- maximum 10-session undercut below the prior 60-session low: 3%;
- 10-session return no worse than -6%, maximum drawdown no greater than 8%;
- current 10-session volatility no greater than 1.10 times the prior window;
- ready-state new-low counts: at most 3/10 and 5/20;
- a new low must be reclaimed within 3 sessions;
- recovery from the 10-session low at least 4%;
- close location in the 20-session range at least 55%;
- close at/above MA20 and non-negative five-session MA20 slope;
- confirmed breakout: positive distance above prior 20-session resistance and
  five-session volume at least 1.20 times the 20-session baseline.

These values are frozen before outcome inspection and are not optimized.

## Boundaries

This task adds a provider-neutral pure kernel and synthetic tests only. It does
not alter S5-A, materialize provider data, inspect returns, write Canonical,
fit a model, register/promote a strategy, or create orders.
