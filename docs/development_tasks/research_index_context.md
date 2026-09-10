# Supplemental research index context — Issue #52

Direct user commission authorizes data download and local canonical writes.
Starting clean pushed master `5784ee7d5820a85292b1e5adb6d83a072ffff819`, master CI
34505551105 passed. Branch `codex/research-index-context`. No concurrent executor
or reviewer. The scheduled agent loop is inactive and untouched.

The source audit found no SSE Composite or STAR50 observations in the original
three-index benchmark partitions. Add a separate data-layer snapshot for
000001.SH, 2019-07-09 through 2026-09-10, and 000688.SH, 2020-01-02 through
2026-09-10. Only these two index metadata responses and calendar-year daily
requests are authorized in this task. Existing daily benchmark files are never
written, merged or replaced. No stock or tradability backfill in this task.

`quantlab.data.index_context` validates provider identity, base/publication
dates, exact SSE calendar coverage, unique identifiers/dates, finite positive
OHLC/pre-close, OHLC consistency and nonnegative optional volume/amount. Missing
volume/amount stays null. Annual requests keep responses small; no undocumented
provider row limit is assumed. All chunks must match expected sessions before
the full snapshot is published. Calendar drift or a failed response rejects the
acquisition. Successful requests are reused without constructing a provider.

One immutable JSON under `data/canonical/research_index_context_v1/<request-hash>.json`
contains normalized observations, provider metadata, request/observation times,
per-request row hashes, reviewed semantics and sources, calendar byte hash, code
HEAD, and a fingerprint over the complete payload. Publishing uses an atomic
exclusive hard link; it never replaces an existing file. Readers validate both
byte-content fingerprints and row/receipt/time semantics. Research consumers
must pin the returned content fingerprint. This is reproducibility evidence,
not a signed provider statement or protection against an attacker replacing all
local evidence. The original wire response is not archived; the snapshot binds
the normalized records returned by the existing provider adapter.

STAR50 rows before 2020-07-23 are explicitly marked as predating publication.
They may provide retrospective warmup after publication, never a signal before
publication. The flag is a lower-bound filter only: downloaded historical
revisions and original per-bar publication timestamps remain **unverified**.
Nothing grants price/execution authority or certifies historical PIT data.

Sources reviewed:

- [Tushare index identity and market codes](https://tushare.pro/document/2?doc_id=94)
- [Tushare index daily fields and units](https://tushare.pro/document/2?doc_id=95)
- [SSE STAR50 publication announcement](https://www.sse.com.cn/market/sseindex/diclosure/c/c_20200619_5130634.shtml)
- [SSE Composite factsheet](https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/detail/files/zh_CN/000001factsheet.pdf)

Run from clean pushed code, under the explicit acquisition authority:

```text
uv run python -m quantlab.data.index_context --authorized-provider-write
```

Self-review checked missing/truncated/duplicate/out-of-range/mixed responses,
calendar identity, publication leakage, unknown-value preservation, artifact
tampering, semantic authority upgrades, observation order, reuse and write
isolation. Pre-run verification: **1,327 passed, two optional default skips**;
both LightGBM/Qlib runtime checks passed separately. Ruff and diff check passed.
Thirty-eight new checks. No runtime dependencies added. Four files changed.

Failures fixed during implementation: the reader initially compared session
coverage fields against the smaller specification mapping; corrected before
first test run. The CLI calendar path was corrected to the existing nested
storage path before testing. No failed live acquisition or source overwrite has
occurred at this stage. Actual acquisition evidence follows after the tested
code is committed and pushed.

No outcomes, labels, model fits, return claims, real account imports, fills,
orders, broker interaction, strategy promotion, C-drive cleanup, or changes to
frozen accounting/backtest behavior. Freeze this bounded adapter after successful
real coverage verification and CI. Next: repair demonstrated historical
ST/suspension/price-limit gaps, preserving missing/uncertain evidence, then use
the preregistered feature/model comparisons. Twenty percent drawdown remains a
research objective, not a verified result or live guarantee.
