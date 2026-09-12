# Fixed31-code dividend raw gap intake

Approved v3 direct data commission; follows the S4 prerequisite audit and PR101
quantity-kernel implementation. Begin from its clean pushed merge and passing
exact-master CI; record the base HEAD. No old writer/heavy worker may be active.
Base95306081d9530b8ccad63e5ee42a421e5d03e0c2, exact master CI34714405197 passed.
Commit this card before implementation; commit/test/push code before actual I/O.

The existing fixed256-stock cohort has225 codes in the older4516-code raw batch.
Acquire only the following31-code set difference (not a newly selected universe):
000301.SZ,000333.SZ,000548.SZ,000753.SZ,000760.SZ,000786.SZ,002366.SZ,002462.SZ,
002474.SZ,002770.SZ,300016.SZ,300105.SZ,300261.SZ,300409.SZ,300471.SZ,600075.SH,
600125.SH,600240.SH,600262.SH,600298.SH,600305.SH,600308.SH,600371.SH,600497.SH,
601009.SH,601126.SH,601139.SH,601318.SH,601579.SH,601668.SH,603181.SH.

Exactly31 reserved/maximum actual requests,zero retries,one segment. Use only
Tushare dividend over https://api.tushare.pro with each exact ts_code and no date
filter; all returned proposal/implementation versions and out-of-window rows stay
raw. All16 existing documented fields:ts_code,end_date,ann_date,div_proc,stk_div,
stk_bo_rate,stk_co_rate,cash_div,cash_div_tax,record_date,ex_date,pay_date,
div_listdate,imp_ann_date,base_date,base_share. Row saturation stop2000; per-share
cash/share ratios remain per share,base_share remains10000 shares. Never interpret
cash_div as the user's final tax result. Add a separate2020–2024 event-date profile;
do not reuse the old parser's2023–2026 window under a new label.

Reuse existing wire client and durable Journal/acquire, with a thin contract/parser
wrapper. Preserve exact raw bytes, observation/request times, hashes, nulls,
duplicate/version candidates and stopped/interrupted attempts. Remove provider
message text from structured reports; retain it only in ignored raw responses.
Missing or empty response is unknown, not proof of no corporate actions. Any
transport, permission, body/row truncation, schema/code mismatch or malformed
non-null measure/date stops the segment with its consumed attempt, no alternate
endpoint or restart. Do not refetch225 previously covered codes or reset the old
4517-request acquisition budget. Its one retry belongs to that older task.

Caps:100requests/minute,2MiB per body,64MiB total bodies,128MiB total ignored output,
600seconds actual acquisition,2GiBRSS,8GiB physicalD reserve,one heavy job. This
implementation/verification part targets25minutes; checkpoint if it cannot close.
No canonical reads beyond frozen manifests/reports, no canonical write, new
formula/candidate/model/economic path, order/account write or promotion. No anns_d,
paid permissions or API aliases. Official dividend docs were checked before the
card; no more than2additional official page reads if needed,zero search expansion.

Bind the256-code selection, audit report/proof,225/31 coverage receipt, old raw
batch report, source-doc observation and this card in the contract. Verify inputs
and clean pushed code before and after I/O. Keep old outputs and parent handoffs
unchanged. Produce one report and a separate checker using raw bytes and counts,
not the writer/parser as the only proof. A successful acquisition does not certify
historical PIT, complete events, payable cash, dividend tax or a tradable strategy.

Tests:exact31 parameters,non-retry behavior via reused journal tests,field and code
validation,nulls and duplicate retention,malformed dates/numerics/saturation,correct
2020–2024 relevance vs old window and no provider-message leakage. Fullpytest,Ruff,
diff,self-review,commit/push,one actual segment,independent verification,Chinese
findings,PR/CI/merge/exact-master closure. Record source/final HEAD,counters/failures,
bytes/timing and remaining event/rule/controller requirements; do not repeat audit.
