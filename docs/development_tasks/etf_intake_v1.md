# ETF input pilot v1

Start at clean pushed master 5eef98b2f5dd71021f78a44c25c1ccb1bf80dd7b after PR93.
Direct user approval of research program v3 authorizes this bounded S1 data task.
No old shared executor/reviewer writes this workspace; their loop stays paused.

Before viewing ETF prices fix these five PROVISIONAL input-audit instruments:
159915.SZ (ChiNext), 510300.SH (CSI300), 510500.SH (CSI500), 511010.SH (government
bond), 518880.SH (gold). They cover distinct types already named by S1; no price,
return, volatility or current liquidity ranking selected them. Current metadata
shows listing before2019, but DOES NOT certify historical existence, names,
benchmark changes or a survivor-free strategy universe. In particular inspect
511010's historical tracking benchmark before any portfolio admission. These five
are neither the final S1 universe nor five strategy candidates; do not use their
current L status as a historical survival filter. D/I/L inventory remains bound.

Acquire exactly20 raw requests: fund_daily, fund_adj, fund_div, fund_share for
all five codes, API order then sorted code order. daily/adj/share use
20191001--20241231; fund_div only ts_code because its official API does not
support a start/end date interval. Preserve its whole response including later
announcements separately, with no later event eligible for a past signal.
No alias retry, pagination expansion, permission probe or alternate API.

Official endpoint docs checked2026-09-12:
- https://tushare.pro/document/2?doc_id=127 fund_daily:5000points,5000rows;
  raw vol in hands and amount in thousand CNY, not canonical shares/CNY.
- https://tushare.pro/document/2?doc_id=199 fund_adj:2000points,2000rows.
- https://tushare.pro/document/2?doc_id=120 fund_div:400points; div_cash per-share
  yuan, base_unit ten-thousand shares. No documented row cap; use conservative
  internal1000-row saturation guard, never call a below-cap response complete.
- https://tushare.pro/document/2?doc_id=207 fund_share:2000points,2000rows,
  fd_share ten-thousand shares. Change date is not publication time.
User has5000points. No8000point ETF endpoints, anns_d or paid independent access.

Reuse reviewed transport/journal with an explicit per-endpoint profile callback;
keep legacy intake behavior unchanged. Save raw bytes, intent BEFORE I/O, hash,
field/row/null/duplicate/date profiles and resource receipt. Exact request IDs,
config and source head bound. One actual segment, max20requests, zero retries,
30rpm,4MiBresponse,64MiBtotal body,128MiBoutput,2GiBRSS,10minutes,8GiBD reserve.
Only ignored launch_20260912/etf_intake may receive new results. Stop on any
transport/permission/schema/saturation error; do not reset partial attempts.
Missing/invalid data stays unknown. Do not infer no distribution from empty data.
No canonical writes, F8 calculation, model fit, economic path or execution.

Independent recomputation of all request identities/raw hashes/profiles and
calendar coverage/invalid numeric counts. Clearly separate raw coverage from
historical PIT, dividend completeness and strategy readiness. A next task must
reconcile corporate actions against official announcements and tracking history,
then seal exact S1 comparison/cost/execution rules before any returns calculation.

Checks: endpoint fixtures including nulls, duplicates, unexpected codes/dates,
truncation, division/quote units, durable budget/replay protections; old intake
regression; full pytest/Ruff/diff. Commit and push before provider calls; attach
actual receipt/proof, PR CI/merge and exact master status. Also make existing
research tables readable (four decimal digits, Chinese column name/local time)
and record Chinese findings and use instructions without changing evidence.
