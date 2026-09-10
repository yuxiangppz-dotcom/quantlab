# Research input quality and lineage — Issue #50

Direct user commission and the preregistered second research round. Start clean
pushed `757672c87473b4ecad5d2e63ca46053bf614e5b0`, master CI 34503342642 passed.
Branch `codex/research-input-quality-audit`. No concurrent coding executor/reviewer.
Use the data-quality skill only as a scoped companion to source readiness;
reproducible inspection lives in `quantlab.research.input_audit`, not a second
standalone analytics workflow. No source connector is needed for local Parquet.

Read 2020-01-01 through 2026-09-10 plus 120 prior sessions. Stream one session's
tables, bind every inspected file and code HEAD, and recheck source hashes at the
end. Check schema, keys, nulls, dates, OHLC validity, positive adjustment/capital
inputs, basic-data joins, known security identifiers, calendar/exchange coverage,
required published index dates and physical context partitions. File presence
does not certify context response completeness or historical tradability.
Current security board classifications and revision/publication evidence remain
unverified. No labels, training, outcomes, provider calls or canonical writes.

CLI: `uv run python -m quantlab.research.input_audit`; requires clean pushed code.
Ignored output: `data/products/research_input_audit/<fingerprint>/audit.json`.
The target is a source-readiness assessment, not a performance acceptance.

Publication boundaries used for index coverage, with no current constituents:

- [SSE Composite](https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/detail/files/zh_CN/000001factsheet.pdf)
- [STAR50 real-time publication from 2020-07-23](https://www.sse.com.cn/market/sseindex/diclosure/c/c_20200619_5130634.shtml)
- [CSI300 publication](https://www.sse.com.cn/market/sseindex/diclosure/c/c_20150911_3984893.shtml)
- [CSI500 factsheet](https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/detail/files/zh_CN/000905factsheet.pdf)
- [CSI1000 reference in the provider's index metadata](https://emt.18.cn/api/quant-help/data/indices.html)

Synthetic checks cover empty/invalid/duplicate/missing grains, calendar gaps and
conflicts, join loss, publication boundaries, insufficient warmup, source drift,
protected code identity and no writes. Full pytest/Ruff/diff/optional runtimes,
self-review and clean pushed real-data audit before PR/green CI/merge/master CI.
Next: repair demonstrated source gaps with preserved original artifacts, then
implement the preregistered bounded features and models. No strategy promotion.

Pre-run verification: 1,289 tests passed, two optional default skips; both
LightGBM/Qlib optional runtime checks passed separately. Fifteen new tests.
Ruff and git diff --check passed. Self-review ensured code HEAD is protected by
the output fingerprint, zero required tables and nonpositive factor inputs are
reported, and the CLI requires a clean pushed revision. The source checker is
ready for the authorized real-data run; findings will be appended after that run.

## Real input audit and interpretation

Executed from clean pushed `2b46065e334d25a296929ab77c5adc1f32f11a22`;
completed `2026-09-11T00:51:41.245480+08:00`. Output
`data/products/research_input_audit/c8beae776bacaf8e0b5b42ddbb48ef347466c66eb48c012c801e1a440950be48/audit.json`.
The 3,913,681-byte artifact binds 9,414 source files (787,024,156 bytes), with no
source-hash changes during inspection. It covers 1,623 target sessions and 120
warmup sessions from 2019-07-09, 1,743 inspected sessions total. Calendar: 12,264
rows; security master: 5,900; code changes: three. No outcome/model was evaluated.

Daily: 8,426,396 rows; adjustment factors: 8,624,420; daily basic: 8,411,869;
existing benchmark indices: 5,229. All four datasets have every inspected daily
partition. No duplicate/null-key, wrong-partition-date, daily OHLC/adjustment
validity, calendar conflict, unmapped security or daily-to-adjustment join
failure was found by these checks. This is local structural evidence, not
independent certification of provider history or historical publication time.

The result is **needs_input_work**, with 341 findings grouped as follows:

| Finding | Evidence and scope | Impact and next action |
|---|---|---|
| High: missing index context | SSE Composite absent on all 1,623 target sessions; STAR50 absent on all 1,490 target sessions from 2020-07-23 | Acquire separately versioned research context; retain the three original benchmark partitions unchanged |
| High: missing tradability context | 1,618 of 1,623 price-limit partitions missing; ST and suspension each missing 406 dates, 2025-01-02 through 2026-09-03 | Bounded resumable acquisition of only missing dates; physical completeness still does not establish full historical tradability |
| High within the affected rows: missing daily-basic joins | 14,771 instrument-days over 321 dates across the raw universe; 14,758 use BJ identifiers and are outside the frozen SH/SZ A-share research scope; 13 SH/SZ A-share rows across four names remain | Keep universe attribution explicit; do not erase terminal/missing observations or silently shrink the research universe |
| High within the affected rows: market-cap inconsistency | 603882.SH on 15 sessions, 2020-09-22 through 2020-10-20, has circulating market value above total market value | Preserve source values; investigate or explicitly mark affected features unavailable, with exclusion/coverage evidence |

The 13 SH/SZ unmatched observations comprise 002604.SZ (four), 300216.SZ (five),
000939.SZ (two), 000760.SZ (two). These counts were recomputed from the retained
finding records, separately from the audit summary. Direct source spot checks
confirmed the 603882.SH inconsistency on 2020-09-22 (44,225,679,286 circulating
versus 44,071,390,536 total CNY) and 2020-10-20 (51,788,844,804 versus
51,608,170,674 CNY). The underlying cause is not established; no repair was made.

Completeness counts for ST/suspension concern their sparse event partitions,
not a full per-security tradability matrix. Historical board membership,
provider revisions, full corporate actions and complete cost evidence remain
unverified. The 20% drawdown objective has not been tested or met by this audit.

Three files changed; no scope deviations. Source checker self-review and all
required tests passed. No provider calls, canonical writes, training, labels,
new return calculations, real account imports, orders or strategy promotions.
Freeze the audited checker after green CI. Next issue should repair the concrete
index/context gaps with immutable provenance, then implement the predeclared
features with explicit missing-data treatment. Final report commit, push and
merge/CI evidence is recorded in the linked PR.
