# Development Notes

## 2026-09-10 — Forward Shadow portfolio alignment

Forward Shadow now delegates exact-count target construction to the core portfolio layer. This keeps the Daily product and forward-evidence paths on the same deterministic selection, tie-break, exposure, cap, and residual-cash semantics while leaving the frozen fractional research constructor unchanged.
