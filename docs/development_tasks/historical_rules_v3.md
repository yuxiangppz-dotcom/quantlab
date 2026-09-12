# Extend historical rule evidence into the fixed pre-2023 period

Approved v3 direct commission, following PR105. Expected clean pushed base:
9dae1bb70cf240e2d70050c2d5511dd7960e0d44, after exact-master CI34724604174 passes.
No old executor/reviewer or heavy worker may write concurrently; retain paused mailbox.

Reuse the existing32 original exchange documents and source hashes. Add exactly8
earlier intervals (2 for each SSE MAIN/STAR and SZSE MAIN/CHINEXT), without
changing the12 v2 intervals, values, source provenance, resolved versions or default
execution rule book. First SSE interval2020-03-13..2022-09-04, second2022-09-05..
2022-12-31. First SZSE interval2021-04-06..2022-08-21, second2022-08-22..
2022-12-31. Retain older missing periods as unknown; do not use2021-03-31 as
SZSE effective date or interpret2022 block-trade amendments as ordinary lot changes.
Keep future amendments out of pre-amendment operative chains; they may certify
the later supersession boundary. Special STAR/ChiNext effective chains stay bound.

Audit the frozen2020-01-01..2026-09-10 calendar:1623 sessions x4 scopes.
Read, do not recompute, the saved v2 audit0875df1c23e547c209658fd8c9440af6f3b0e22feeb487231dd80b73c0cbba28.
Prove all895x4 prior rows unchanged and all prior resolver payloads preserved;
report actual earlier coverage/gaps and full2022 coverage explicitly. No window
choice from returns, no provider/market-data calls or canonical writes, no models,
economic paths, candidate identities or promotion.

Finite financial source review: reuse local original bytes, at most4 public opens
of previously successful official effective-date notices;0searches/0downloads/
0retries, no revisit of previously failed links. Bind the review receipt and all
parent contracts/code/calendar/report. Preserve old actual/proof outputs.

This remains a retrospective continuous-auction LIMIT-order subset: ticks, ordinary
buy/sell lot minima/increments/maxima, full residual exit and earliest ordinaryT+1
lag. It does not certify closing/opening auction execution, cages, price bands,
ST/suspension, security classification, cash/fees/corporate actions, broker balances
or fills. Never label the completed subset a full historical trading controller.

Bound one actual audit and one separately coded saved-output proof, neither rerun;
20minute implementation/audit segment,2GiB RSS,64MiB new output,8GiB D reserve,
one heavy job,2threads,600seconds per worker. Synthetic mutation/boundary tests,
fullpytest/Ruff/diff, self-review, commit/push before actual, PR/CI/merge/exact-master
closure before another independent part. Freeze any source gap, do not expand
the card or stall independent approved strategy research. Record heads, hashes,
attempts, failures, timings, limitations and precise next step.
