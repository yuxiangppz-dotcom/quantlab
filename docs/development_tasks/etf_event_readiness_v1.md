# ETF event readiness, bounded audit and reviewed display

Direct user-approved v3 commission. Clean pushed starting HEAD:
`4a44196225e23bad7940fb0516b29787f2cc10e6`.
The existing handoff card authorized this read-only audit and its UI integration.
No other executor/reviewer/heavy worker was writing; the old shared loop stays paused.

## Scope and observed results

The audit sealed 64 existing input bindings and performed 12 targeted public searches
and 8 distinct public-document GETs: 7 PDFs, one HTTP403. The failed URL was not
retried. Output is about 14 MiB, below 256 MiB; no provider request, canonical write,
return calculation, F8 calculation, model fit, order, fill or promotion occurred.
First-phase research was closed within the 45-minute card limit.

Ignored evidence root:
`data/products/research_program/launch_20260912/etf_event_audit`.
Report fingerprint: `22e8a11424275377172eb74195ec4396cae8c280ceb47d113445911d49c58e2a`.
Separate arithmetic/source proof: `fbc4e237c6f327957ece5bb91e51e491d3e1d4c3ceed6d0b85d0d844619e7d37`.
This is same-agent self-review with a separate checker, not a second human/agent review.
Raw files, extracted pages, row references, search descriptions and source receipts
remain local and ignored. Search descriptions preserve intent/domain filters;
exact per-query timestamps and original search-response bytes were not retained.

- The 38 dividend rows form 24 equivalent event-key groups. Of 14 repeated groups,
  5 are exact duplicates and 9 differ in auxiliary metadata. Cash per unit and
  record/ex/pay/announcement terms agree within these observed groups. This
  permits an audit of cash equivalence, not arbitrary canonical deduplication or
  historical-version certification. All originals remain intact.
- CSI300 ETF yearly cash per unit for 2019–2024 reconciles with the annual reports
  after converting the source's **per ten units** measure. The 2020 table has
  dashes; this supports absence only in that distribution table's stated scope.
  The 2024 report confirms record/ex dates but not payment date. The 2021 original
  table puts its ex-date in the off-exchange column; preserve the ambiguity.
  Sources: [2024 annual report, pages 8 and 45](https://www.sse.com.cn/disclosure/fund/announcement/c/new/2025-03-31/510300_20250331_07Z1.pdf),
  [2021 annual report, pages 8 and 71](https://www.sse.com.cn/disclosure/fund/announcement/c/new/2022-03-31/510300_20220331_2_q8W7xQBl.pdf).
- ChiNext and gold ETF annual distribution sections support no distributions
  in 2022–2024. This does not establish absence in 2019–2021 from empty API rows.
  Sources: [ChiNext annual report, page 9](https://static.cninfo.com.cn/finalpage/2025-03-31/1222961600.PDF),
  [gold annual report, page 8](https://www.sse.com.cn/disclosure/fund/announcement/c/new/2025-03-31/518880_20250331_1BBG.pdf).
- CSI500 ETF's split plan supplies a 1.14539 ratio and rounding up of the holder's
  aggregate quantity. Raw shares change on Aug26 while raw quote units/adjustment
  change Aug29. Printed four-decimal factors are numerically compatible under an
  explicit rounding assumption, not a certified exact-ratio substitute. Actual
  split results and the 2024 dividend notice remain unresolved. The Aug25–26
  restriction is for agreed repurchase, not secondary-market suspension.
  Source: [split plan, pages 1–2](https://www.sse.com.cn/disclosure/fund/announcement/c/new/2022-08-23/510500_20220823_1_aMMa3ZNB.pdf).
- The 511010 Q1 2020 report already specifies the five-year government-bond index.
  No benchmark change has been established. Source: [Q1 report, pages 2–3](https://www.sse.com.cn/disclosure/fund/announcement/c/2020-04-21/511010_20200422_1.pdf).

## Open conditions and next decision

The matrix records all five products as not yet admitted to S1 economic evaluation.
159915 still lacks an explained 20210208 price. Corporate-action completeness,
payment chronology, historical identity/benchmark coverage and execution-rule
admission remain product-specific open conditions. Unknown is not a suspension,
zero dividend, executable forward-filled quote or permitted fill.

F8 also lacks publication/revision timing. That issue does **not** block S1-A/B,
which do not consume fund_share. No full-chain PIT inference is made from later
annual reports, and retrospective event accounting is kept separate from signals.

`s1_comparison_preconditions_v1.md` freezes the intended comparison and budget
before returns, but is expressly not a runnable return card until input/rule gates
and an exact pushed code binding exist. Do not repeatedly reopen this completed
12-query audit; use a separately bounded independent work package next.

## Implementation and checks

The existing 策略研究进度 page now shows the reviewed ETF matrix and downloads.
Its reader checks pinned report/proof/parent links, all source/artifact hashes,
available byte counts and explicit false authority/admission flags. Missing proof
hides results; tampering or unsupported authority displays a validation error.
It cannot start collection, economic evaluation, account writes or orders.

Focused reader and existing progress tests passed (39 tests); full pytest passed
2074 tests, with10 optional-runtime skips and56 existing NumPy warnings. Ruff and
whitespace checks passed. The actual saved-evidence Streamlit check rendered four
tables with zero exceptions/errors. PR/CI and exact-master results are recorded
in the final local handoff after push; no generated evidence is committed.
Browser-bridge inspection returned `nodeRepl.fetch request failed`; use the actual
saved-evidence Streamlit acceptance check as fallback and do not report a new
visual browser acceptance as passed.
