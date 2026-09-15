# S5-B v1.6 pure diagnostic metric kernel

Issue: #151. Starting master:
`5ddc394ac35d7ad01fd50d5f71fc551a2594112c`.

This task consumes only the already-admitted
`S5BaseDiagnosticInputPackage` and produces deterministic retrospective
statistics. It freezes complete-population state counts, adjacent-observation
state transitions, daily allocation summaries, state return distributions,
date-paired cohort and comparison spreads, and selected-signal monthly and
broad-market regime breakdowns.

Every statistic is a close-label diagnostic. The 1/5/10/20 horizons are
observation windows, not minimum holding periods. Empty cohorts remain explicit
rather than being silently removed, and all date-paired spreads give each
eligible signal date equal weight.

No provider access, Canonical read/write, real outcome materialization,
parameter scan, holding-policy choice, executable return, performance claim,
promotion, account mutation or order is authorized. Tests use synthetic input
packages only.
