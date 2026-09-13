# Risk-ledger final review follow-up

Direct single-agent integration commission; scheduled automation remains PAUSED.
Expected clean pushed main HEAD: `6521ffc6f14ff117aaf46ef0b9717b8410c87661`
(PR #124 merged; reviewed head `1217bd5d0e21cd8eb437d451f07f7886f5b36052`).
No old executor/reviewer found writing main. This task uses an independent
worktree and leaves ZCode and earlier review directories untouched.

## Scope and reason

The engine passed independent post-execution missing-mark and V-input probes,
including partial buys and limit-down sells, and the full 3099-test suite.
Two checked-in regression tests do not reach their named scenario: the V test
stops before any trade because it has no warmup; the partial/blocked test drops
an entire future evidence batch before execution. Replace them with real
post-execution gaps and compare complete checkpoints and resumed daily records.
Add adequate V warmup, assert the clean run actually partially fills or blocks,
then remove the current risk observation and verify atomic rollback and recovery.
Keep nonzero declared synthetic fees. Preserve every other regression.

The Chinese guide still calls Checkpoint.start(run_id=...) after the API was
changed to start(config=...). Update it to create one immutable config and
reuse it for checkpoint and run, describe full configuration binding, and label
the abbreviated snippet as an interface sketch with caller-supplied evidence.

No production financial code change, provider call, canonical write, historical
experiment, model training, registry promotion, external order, or automation
resume. Existing sources/budgets remain frozen. Do not re-run sealed writers.

## Acceptance and delivery

Focused changed tests, then uv run pytest, uv run ruff check ., git diff --check.
Inspect the doc example against the public API. Commit and push this finite
part, create its PR, self-review, wait for exact-head CI, and merge only when
checks pass. Report starting/final heads, commits/push, changed files, validation,
failures and limitations. Synthetic completion is not historical performance;
G1-G5 admission gaps remain open. No new strategy work in this task.
