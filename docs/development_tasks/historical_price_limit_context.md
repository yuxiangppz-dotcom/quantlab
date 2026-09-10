# Historical stock price-limit scope and repair — Issue #56

Direct user commission authorizes the exact provider reads and canonical data
repair described in Issue #56. Start clean pushed
`9117a795f8868c4ff30ad324e9b5ddb9acf611c5`, master CI 34509162043 passed.
Branch `codex/historical-price-limit-context`; no concurrent executor/reviewer.
No scheduled-loop, broker, account or frozen backtest/rule changes.

## Five-response preflight

The read-only preflight ran from the starting revision on 2026-09-11 at
01:40:10.600781–01:40:15.851392 Asia/Shanghai, with exactly five stk_limit calls.
Its concrete script is retained as `historical_limit_preflight.py.txt`; the
script's fixed starting HEAD and output-directory checks prevent accidental
reruns from a changed revision or silent replacement of its evidence.

| Date | Provider rows | Daily current-master A-share scope | Daily scope including valid old codes |
|---|---:|---:|---:|
| 2020-01-02 | 3,801 | 3,740 | 3,741 |
| 2023-01-03 | 5,110 | 4,899 | 4,900 |
| 2025-02-14 | 5,478 | 5,121 | 5,122 |
| 2025-02-17 | 5,477 | 5,125 | 5,125 |
| 2026-09-03 | 5,634 | 5,209 | 5,209 |

Every requested historical-scope instrument had a provider limit row. No
duplicate identifier, wrong date or invalid/nonpositive limit in that scope
was observed in these five samples. This is not a claim about unsampled dates.
The existing verified 300114.SZ → 302132.SZ change became effective 2025-02-17.
Before that boundary, **the provider returns the valid old-code limit, but the
old current-master filter would discard it and also exclude it from coverage
checking**. The probe therefore established a concrete data-selection defect.

Probe output `data/products/price_limit_preflight/20260911/report.json`,
fingerprint `9fdf77752fe832a67d8d954700999147c946275166f7298ff671bcd5c515406a`.
Normalized responses, exact observation times and local daily/master/calendar/
code-history hashes are bound. The five original canonical limit partitions
were byte-unchanged. No canonical data was written during the probe.

## Correction and bounded acquisition

`sync_daily_price_limits` now uses the already established code-history dates:
retain an old identifier within its validity window, exclude a successor before
its effective date and an old code after it, and never rename source records.
Any remaining unverified historical SH/SZ A-share identifier in raw daily data
blocks acceptance rather than disappearing from the completeness denominator.
BJ/B-share scope, numerical validity, exact dates, missing-limit rejection and
immutable existing partitions remain unchanged. Financial/dividend acquisition
still uses its original current-master scope; no new identity facts are inferred.

`quantlab.data.limit_backfill` requests at most 1,618 missing dates,
2020-01-02 through 2026-09-03, serially paced at 0.4 seconds minimum separation.
It reuses the strict history-aware sync helper and the tested job-local exclusive
Parquet publisher. Existing partitions remain untouched. Per-call normalized
response hashes and times precede acceptance receipts. Invalid/missing/duplicate
responses are retained in separate diagnostic JSON with `tradability=unknown`,
and the job continues to inspect other planned dates. Provider/credential errors
stop the run. No unknown limit becomes zero, infinity, an unlimited-day assertion
or a synthetic executable price. Unknown nonfinite source values remain explicit
tagged values in diagnostic evidence.

The run binds calendar, security/code history, every inspected daily partition,
all previous limit files and every accepted/rejected output; final hashes detect
drift. An incomplete run stays partial, resumption only requests absent dates,
and concurrent publication is a failure rather than an overwrite. Original
per-bar publication/revision history and complete historical tradability remain
unverified. There is no order, performance or drawdown authority.

[Provider endpoint/fields](https://tushare.pro/document/2?doc_id=183). Prices are
reported in CNY. The existing adapter rejects the documented 5,800-row response
boundary and filters stock asset rows. Stock facts are not ETF limit authority.

CLI, from tested clean pushed code under the exact active commission:

```text
uv run python -m quantlab.data.limit_backfill --authorized-provider-write
```

Pre-run validation: **1,369 tests passed, two default optional skips**, both
optional LightGBM/Qlib checks passed separately; Ruff/diff checks passed.
Twenty-two new checks cover old-code boundaries, missing old-code coverage,
invalid/duplicate/empty limits, scope/call budgets, partial/resumed acquisition,
provider failures, immutable reuse and source drift. Self-review tightened
unmapped-identifier rejection, fixed a closure binding flagged by Ruff, added
diagnostic-output hash rechecks and suppressed provider-initialization details.
Six files changed, with no new dependencies. Tested code is committed/pushed
before bulk acquisition; real evidence and final status follow below.

No calls outside stk_limit, no securities/calendar changes, financial/dividend
downloads, labels, models, return claims, account changes, fills, orders,
strategy promotion or C-drive cleanup. Next: predeclared wick/MACD/index feature
and bounded model diagnostics, explicitly masking unresolved data; executable
net-return and 20% drawdown conclusions remain blocked by missing evidence.
