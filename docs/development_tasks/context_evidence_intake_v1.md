# Bounded S2/S3/S5 prerequisite intake:43 fixed requests

Direct user-approved v3 data commission. Implementation base is clean pushed
master `5fa5adcd6344de3591a44157d3c7e41781404a51`, CI34708019982 passed. The preceding
read-only S2/S3 audit is complete and sealed; do not repeat it or its six searches.
No old executor/reviewer is writing; old shared mailbox remains paused.

This card authorizes a new staged raw-data intake, implementation and tests,
commit/push **before** actual requests, one actual segment, independent receipt
review and a findings document. No canonical write, model fit, formula, economic
path, original-API alias probe, paid permission, anns_d, order or promotion.
Do not alter the existing five-column DailyBasic/lifecycle/backtest semantics.

## Exact requests — no result-dependent selection

Use the existing configured Tushare credential and `https://api.tushare.pro` only.
The eight codes are positions0,32,64,96,128,160,192,224 of the existing sealed,
sorted256-code2019 cohort:
000004.SZ,002024.SZ,002517.SZ,300022.SZ,300558.SZ,600303.SH,600845.SH,603127.SH.
They are an input-feasibility sample, not a trading universe or event shortlist.

1. daily_basic,11 calls, one per trade_date:
   20191231,20200102,20201231,20211231,20220104,20221230,20230103,20231229,
   20240102,20241231,20260911. Fields:ts_code,trade_date,close,turnover_rate,
   turnover_rate_f,volume_ratio,pe,pe_ttm,pb,ps,ps_ttm,dv_ratio,dv_ttm,total_share,
   float_share,free_share,total_mv,circ_mv. Stop at6000 rows, not an all-history
   completeness claim. Preserve provider percentages/万元/万股 units in raw rows.
2. index_member_all,16 calls: each fixed code × is_new Y/N. Fields:l1_code,l1_name,
   l2_code,l2_name,l3_code,l3_name,ts_code,name,in_date,out_date,is_new.
   Stop at2000 rows. Historical intervals and blank out dates are observations,
   not automatic historic membership certification or permission to backfill.
3. forecast,8 calls, each code,start_date20200101,end_date20241231. Fields:ts_code,
   ann_date,end_date,type,p_change_min,p_change_max,net_profit_min,net_profit_max,
   last_parent_net,first_ann_date,summary,change_reason. Stop at3500 rows.
4. express,8 calls, same code/date window. Fields:ts_code,ann_date,end_date,revenue,
   operate_profit,total_profit,n_income,total_assets,total_hldr_eqy_exc_min_int,
   diluted_eps,diluted_roe,yoy_net_profit,bps,perf_summary,is_audit,remark.
   Local safety stop at1000 rows; this is not a verified provider row-limit claim.

The source documents indicate2000-point single-stock/member access and5000-point
quarter-wide financial variants; this card uses only the exact non-VIP endpoints
above. Do not try an alternate endpoint after a denial. Empty responses remain
unknown; duplicate keys, announcement variants and metadata conflicts are retained.
No signal is computed, and no latest corrected value becomes a first-announcement
value. PE null for losses is not a zero, and current snapshots cannot prove a
no-revision historical policy.

## Caps and review

Exactly43 reserved request identities; max43 actual requests,0 retries,1 segment,
100requests/minute. Per response2MiB, total raw-body budget64MiB, total new output
128MiB,2GiBRSS,600seconds,8GiB physicalD reserve. Preserve lossless raw bodies,
request/response hashes, actual request/observation times and source fields.
Unexpected schema, code/date scope, truncation, saturated row count, permission,
transport or resource failure stops the segment. Started/failed attempts consume
their slots; never erase or restart to recover silently. A partial batch is valid
evidence of partial acquisition, not an excuse to report43/43 success.

Reuse the existing raw journal/wire client and heavy-job OS lock. No token is
logged in reports or Git. Scope and row inspection are read-only; no normalization
is written to formal data. Before and after requests verify all bound inputs and
clean pushed code identity. Save request/result profiles, counts and a separate
checker proof. Any response message remains in the ignored raw body, not logs.

Synthetic checks:43 unique exact parameters, no missing history flag, per-field
schema, code/date mismatch, duplicates retained, unknown dates, non-finite/boolean
numeric values, row/byte truncation, permission failure and no retries. Full pytest,
Ruff/diff, self-review, commit/push, actual bounded intake, independent response
review, PR CI/merge/exact-master closure. A data result does not authorize S2/S3/S5
economic paths; register the next decision after inspecting the evidence.
