# Guided local workbench — Issue #40

Direct user commission to iterate toward a clearly usable personal QuantLab tool.
Exact clean, pushed starting master: `8e50306fe6fb2fc8c289c8950e718b1cddf77470`.
Branch: `codex/guided-local-workbench`. Prior update PR #39 passed both CI jobs.

Reuse existing services to deliver a Chinese start page, explicit bounded data
updates, prospective observation management, cash-flow preview/import and immutable
valuation access, preserving ranking/backtest/account/reference-plan pages.
Unknown or corrupt evidence must be shown as such. Templates contain headers only,
and a stale account/source preview cannot authorize a later import. Page load is
read-only; all materialization/import/provider actions require a user action.

Deliver a local-only Windows start/stop launcher and desktop shortcuts, Chinese
quick-start/full guide and actual user-path validation. Synthetic tests cover
empty/corrupt state, eligibility, secret redaction, preview binding and page/action
rendering. Test launcher lifecycle and browser health. Full pytest/Ruff/diff-check
and optional runtimes, PR CI and post-merge checks remain mandatory.

The user explicitly authorized data processing, Tushare downloads and model
training during this task. This acceptance uses only the previously specified
five-session update through 2026-09-10 and calendar through 2026-10-15; no training
or financial/dividend history download is needed for this delivery. Product
materialization and a separately named, clearly simulated acceptance account are
authorized as local validation. No real account records, orders, strategy
promotion, historical training, financial-rule changes or loop changes are in scope.
Corporate-action accounting, historical state replay, full performance readiness
and broker integration remain explicitly incomplete. Final evidence belongs in
the PR handoff and delivery record, with exact starting/final HEADs.

Self-review also found that a new opening snapshot replaced the only saved basis,
and the demo button could reuse a manual account id. Preserve opening snapshots
by fingerprint and reject changing an existing account's mode. Old journal
bindings remain unchanged; never replay one basis's journal against another.

## Local verification before PR

- Full regression: 1,209 passed, 2 optional tests skipped; explicitly enabled
  Qlib/LightGBM runtime tests: 2 passed. Ruff and diff whitespace checks pass.
- A fresh headless Edge session visited all seven pages with no application or
  browser JavaScript exception. Actual temporary-journal tests exercised both
  fill and cash-flow preview/commit paths; previews did not write journals.
- Windows launcher reuse, owned-process stop and fresh restart passed. Both
  desktop shortcuts were installed and their target/arguments verified.
- Authorized update advanced complete core/index data to 2026-09-10; context
  and limit requests succeeded. Seven checked old partition hashes were unchanged.
- A test-harness radio click initially hit Streamlit's styled radio overlay;
  clicking its visible label fixed the harness. An initial cross-folder fixture
  import and one lint line length were corrected without relaxing assertions.
- Final clean-master daily, prospective registration, demo plan and valuation
  acceptance remain post-merge checks. All generated evidence stays Git-ignored.
