# Issue #64: bounded historical Alpha158 staging

Direct single-agent commission, beginning at clean pushed master
b361363012a38916e8d9dc0ee1b51f007f032061, successful master CI 34558914342.
No concurrent executor/reviewer or heavy research job was found. D had
107,920,990,208 bytes free and WSL 14,606 MiB available RAM at preflight.
No old shared agent loop is active.

Before feature outcomes, the fixed scope is 2020-01-01 through 2026-09-10,
plus 120 prior market sessions. Inventory-only source inspection found 5,443
SH/SZ v1 historical observed codes, 1,743 sessions starting 2019-07-09,
3,489 source files, 348 outside-v1 observed codes, no missing daily/factor
partitions and no unknown lifecycle identities in the available master/history.
This is observed local coverage, not proof of complete historical market data.
The source inventory is sealed with fingerprint
0869cd29048ef0649838ebd6f6c846e5ccd9174e90498a95941ae1b21cc6649c.
It reads no factor, label or return outcomes. The final source/mapping/resource
contract is committed and pushed before any native historical feature batch.

Use the previous pinned Qlib 0.9.7 Alpha158 mapping contract unchanged. Preserve
raw observed rows, calendar identities, native output and stricter usable masks.
Unknown lifecycle retains a nullable status and cannot acquire usable inputs.
Historical observed identity discovery never filters by current status or future
labels. Curated code changes remain separate; no predecessor/successor stitching.

Monthly raw partitions feed fixed lexicographic batches of at most 32 codes.
All committed partitions are immutable and source/code/config/runtime bound.
Exclusive OS locks prevent concurrent workers. JSON receipts publish by atomic
exclusive hard link after data hashes are known. An abandoned attempt can resume
once into a separate retained attempt; all partial bytes count toward one budget.
A recorded terminal error requires explicit technical recovery, never an automatic
retry loop. Completed partitions are verified and reused without recomputation.

Whole-batch resource ceiling: 16 GiB new files, 6 GiB process RSS, two computation
threads and one heavy job; reserve at least 8 GiB of host D space outside the
remaining batch allowance. Check a conservative next-partition disk/memory
projection before writing. A 100 ms watchdog limits the isolated worker's RSS
and wall time, preserving partial evidence; no unrelated process is stopped.
Allow at most 45 minutes heavy work per wake-up, stop at checkpoints with three
minutes left to avoid starting a large partition near the deadline. Resume the
same identity and cumulative budget. Record all attempts and cumulative wake time.

Numerical overlap with the frozen five-code target interval requires exact keys
and missingness, rtol=1e-5 and atol=1e-6 fixed before results. Do not loosen these
after observation. Each batch's first lexicographic code supplies a deterministic
160-session sample beginning at its first finite mapped close (capped to keep
160 sessions where available; calendar start if all missing). Check file/memory
native parity, 101-session prefix invariance and future-field/label perturbation.
These sample checks supplement the full overlap and existing independent native
synthetic fixtures; they do not independently reimplement all 158 formulas.

No research fits, IC optimization, return evaluation, provider API, canonical
write, account/order/fill, retrospective forward registration, promotion,
purchases, C-drive cleanup or old agent-loop modification. Existing accounting,
full-cost, rule/accessibility and historical availability limits remain.

Validation includes atomic/interrupted/tampered/mismatched/concurrent partitions,
resource checks, unknown lifecycle, missing calendar rows and actual Qlib synthetic
batch execution. One initial synthetic assertion expected every correlation to
exist despite almost constant volume changes; CORD5/CORD10 correctly stayed
undefined. The integration fixture was changed to explicitly varying volume to
exercise its intended fully usable-row case; real outcomes/tolerances were not
changed. Full tests, optional checks, browser acceptance, PR/green CI/merge/master
CI and the next fixed rolling-model task are required.

Before the real feature run: 1,428 default tests passed, 4 optional tests skipped
by default, and all 4 explicitly enabled runtime tests passed. Ruff and diff
checks passed. The source-bound contract SHA256 is
e25fd138b4fdfccd504d80a76ac71301f2f72f7f7214a01253618430d5750a59.
The Chinese view reads only sealed summaries and supports checkpoint progress.
