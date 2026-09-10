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
