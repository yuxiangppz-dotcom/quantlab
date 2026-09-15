# Alpha158 saved-model replay intake

Starting HEAD: ad5e16147929e8f6a35ede10936086c1adf691d6. Task card: 3a6513f.

The intake verifies four saved score receipts, excludes future labels from ranking, and fixes daily Top-20 equal targets with next-session execution. It does not produce a portfolio NAV.

Actual v2 audit: 2023-01-03 through 2026-09-10, 17,900 target rows, 3,868 instruments, 2,221 ST target rows. All selected instruments have hash-verified archived dividend responses. These receipts do not certify complete cash-flow entitlement history. The reviewed v2 historical rule catalogue resolves the checked quantity scopes; using the default execution rule book alone would incorrectly report missing rule coverage.

66 target execution quotes are absent. A separate local partition check finds an S suspension record with empty timing on all 66 dates. This supports a suspension interpretation, not fabricated execution prices. One target, 300114.SZ on 2024-11-27, lacks current securities metadata. Current metadata is not historical identity proof.

The existing scheduler requires raw current-day marks for held and intended securities. Full-period replay still needs explicit suspended-position valuation and blocked-order integration, record-date entitlements, payment-date receivables, share availability and disposal-dependent dividend taxation. No synthetic quotes or zero unknown corporate cash were inserted. No portfolio return, drawdown, Sharpe, or outperformance claim is available.

Audit v1 failed because the generic sealed loader was used for the index snapshot, which has a different hashing contract. Partial output is retained with failed.json. Audit v2 uses load_index_context with the pinned fingerprint and completes. Its output remains local under data/products/model_replay/alpha158_lgb_20260915_intake_v2.

Checks before final loader/catalogue correction: 3516 passed, 10 skipped, 56 existing warnings. Final verification recorded in delivery. No downloads, canonical writes, fits, economic paths, orders, or automation changes.
