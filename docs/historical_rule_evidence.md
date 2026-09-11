# Historical exchange-rule evidence v1

Issue #68 reviews a fixed, sealed 895-session calendar (2023-01-01 to 2026-09-10)
for SSE MAIN/STAR and SZSE MAIN/CHINEXT. This is a retrospective source catalogue,
not an execution-rule promotion. The default resolver and personal fallback remain
byte-for-byte unchanged. No instrument's historical board is inferred here.

`config/historical_rule_catalogue_v1.json` declares all eight reviewed intervals,
values, provision references, effective/supersession evidence, limitations and
resource limits. `config/historical_rule_sources_v1.json` pins the contract bytes,
24 downloaded official source documents, separate retrieval timestamps, failed
retrievals, sealed calendar and the two frozen implementation files. Raw documents
remain ignored under `data/products/rule_evidence/rule_catalogue_20260911/sources`.
Commit and push this contract before running `uv run python scripts/audit_historical_rules.py`.
The runner refuses an existing start receipt and a dirty or unpushed worktree.

The 2023 rules take effect on 2023-04-10, when the first registered main-board IPOs
actually listed. Their publication date is 2023-02-17. Both 2026 editions take effect
on 2026-07-06 and supersede the 2023 editions that day. The 2026 interval ends at this
audit's September 10 cap, not a legal expiry. Later notices bound retrospective
supersession; they are not treated as information available at the earlier start.
The SSE deferred-provision lists and 2026 explanatory memoranda were inspected;
their unimplemented/out-of-scope mechanisms do not become supported order types.

The catalogue intentionally leaves January 1–April 9, 2023 unknown. Old primary
documents were retrieved but the complete older amendment/special-rule chain was
not certified in this version. Baseline-only coverage is reported as still unknown
in the new catalogue, never counted as newly verified. This is an evidence gap,
not a statement that these markets were closed or lacked trading rules.

Modeled fields cover the ordinary continuous-auction limit-order subset: tick,
ordinary buy/sell quantity minimum and increment, per-order maximum, full odd-lot
exit and the earliest ordinary one-session sellability lag. T+1 is supported by
the no-resale-before-settlement clauses, their enumerated same-day exceptions and
exchange explanations/history. It does not prove account settlement, unlocked
holdings or a fill. The 2014 sell-quantity notices support a larger set of legal
odd-lot splits than the frozen implementation; this catalogue preserves only its
conservative regular-lot/full-position subset and discloses that restriction.

The retrieved SZSE 2021 notice explicitly states April 6, 2021 effectiveness, while
the frozen resolver starts that version March 31 (publication). This discrepancy
is outside the audited date range and remains an explicit blocker for any later
legacy-rule migration. This task does not silently repair its old version.

The report includes all scope/date rows, interval groups, baseline/new/unknown
counts and value conflicts. Unknown stock identity, market access, intraday status,
price bands/cages, fees, corporate actions, liquidity and fills remain separate
prerequisites for the fixed 5/10/20-session and hypothetical capital scenarios.
No training, Tushare/canonical writes, orders, account changes, forward registration
or performance/drawdown claims are part of this audit.
