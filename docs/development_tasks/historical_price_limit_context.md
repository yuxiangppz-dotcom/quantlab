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

| Date | Provider rows | Raw daily/current-master overlap | Raw overlap plus known old codes |
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
code-history hashes are bound. These preliminary overlap counts still include
known successor-code backcast bars; they are not the final PIT coverage set.
The five original canonical limit partitions
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

## First bulk run: failed scope check and correction

The first bulk attempt ran from pushed
`7aa70894995d1c8cb2c6552733bc8f61318e9106`. It made 244 provider attempts, retained
243 responses through 2020-12-31, and accepted **zero** canonical partitions.
The worker was explicitly interrupted after repeated identity-scope rejections;
the final in-flight attempt has unknown response outcome and counts against the
request budget. Report:
`data/products/limit_backfill/f1ff6c6489f94bc1b42f7966152e4639/report.json`,
fingerprint `5b27f86c9c5090dce3860c001bfe15e3731e76e500a82c4be29523abea204c58`.
All bound sources and original limit files remained unchanged. The original
report's status `complete_with_unresolved_dates` failed to distinguish operator
interruption; that limitation is recorded here without altering its fingerprint.

The live diagnostic exposed a mistake in the newly added unknown-identity guard:
raw daily history retains both 300114.SZ and its successor's backcast 302132.SZ
before 2025-02-17. The shared research PIT builder already excludes that inactive
successor by its effective date. The new guard incorrectly treated it as unknown.
The correction applies the same explicit code-validity dates to **both** returned
limits and the expected daily identities. Known inactive codes are excluded;
genuinely unverified identifiers still block acceptance. Tests now include both
raw codes on either side of the date boundary, matching the actual data shape.

Interruptions are now explicitly recorded, including unknown in-flight outcomes;
an unverified identity stops for review instead of downloading hundreds more
dates with the same structural issue. `limit_response_replay` revalidates pinned
saved responses without a provider connection, preserves their original request
and observation times, and records separate revalidation/publication receipts.
It verifies source report/response hashes, original fixed/daily inputs and final
outputs, and retains strict rejection behavior. Source-report order determines
duplicate-date selection explicitly; no automatic fallback to a favorable version.

Recovery will first use the 243 retained bulk responses and four additional
nonduplicate preflight dates. The January 2020 duplicate uses the first-listed
bulk report. At most **1,374 additional provider attempts** remain after the
244-attempt interruption; the CLI now accepts a smaller explicit call budget.
No failed normalized response or original report is deleted or rewritten.

Recovery-code verification: **1,378 tests passed, two default optional skips**;
both optional runtime checks, Ruff and diff check passed. The task now includes
31 new checks and eight changed files. The added checks cover inactive-code
backcasts, explicit interruption, pinned offline replay, source tampering/drift,
unchanged observation times and deterministic response-version selection.

## Completed recovery, acquisition and verification

Both successful phases ran from clean pushed
`81b88570d63d4d454204cabbe0723662de9bdebf`.

| Phase | Local time on 2026-09-11 | Accepted partitions | Rows | Canonical bytes | Provider attempts |
|---|---|---:|---:|---:|---:|
| Revalidate saved responses | 02:02:01.128044–02:02:32.616286 | 247 | 970,182 | 84,182,466 | 0 |
| Remaining missing dates | 02:03:20.806659–02:22:10.778820 | 1,371 | 6,736,848 | 585,446,880 | 1,371 |

All 1,618 missing partitions were accepted: 7,707,030 rows and 669,629,346 bytes.
There were no remaining invalid/rejected dates or detected source-hash changes
in either successful phase. The initial 244 attempts plus the final 1,371 equal
1,615 additional attempts, within the authorized ceiling of 1,618 (the five
separately authorized preflight reads are separate). The interrupted in-flight
attempt is conservatively included. All earlier responses/reports remain intact.

The offline report is
`data/products/limit_response_replay/8c054fcf19a842728887afac23fe8282/report.json`,
fingerprint `546abce06d7d4a7e2452e0784fc5c74c79ee79880a442c981b9980f7e2ede238`.
The completed download report is
`data/products/limit_backfill/e80dc7610e734a94bb5cb8b4943d6525/report.json`,
fingerprint `a4e123ea9243206e8054092a0187777c44b0456a8fca37a917135aa92dc5d426`.

Independent verification recomputed both final report fingerprints, all 1,618
accepted-file hashes and all 252 limit files that preceded the final phase,
including the five original recent partitions. All matched. Calendar comparison
confirmed all **1,623 target sessions from 2020-01-01 through 2026-09-10** have a
price-limit partition. A zero-budget rerun completed with zero provider calls,
zero accepted/rejected files and zero missing dates; its report is
`data/products/limit_backfill/19d649bfed634742aa772e760a4b6302/report.json`.

Direct reads also confirmed 300114.SZ present and 302132.SZ absent in the accepted
2020-01-02, 2023-01-03 and 2025-02-14 partitions, with the reverse on 2025-02-17.
Original raw daily files were not renamed or rewritten. This is date-correct
local data selection and source coverage, not proof that every recorded limit
constituted an enforceable exchange restriction or that every stock was tradable.

Freeze the corrected bounded acquisition/replay after green final CI. The actual
bulk-run defect, safe interruption and recovery are fully recorded above; no
failed attempt is hidden or relabelled successful. The eight files, all code and
evidence commits are pushed. Final report commit and PR/master CI are recorded
in the PR. The mainline now proceeds to the preregistered research features and
model diagnostics; financial and execution limitations remain unchanged.
