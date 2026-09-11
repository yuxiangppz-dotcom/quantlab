# Historical rule evidence v2: the pre-April-2023 chain

Issue #70 adds exactly four separately versioned research intervals for
2023-01-01 through 2023-04-09. The eight v1 intervals, their source metadata, the
v1 report and the frozen default/personal behavior stay unchanged. The v2 loader
checks those bindings and rejects changes outside this declared early gap.

Source review reuses 24 hashed v1 originals and adds eight official documents:
SSE 2020 republication notice, its amended-clause and deferred-clause attachments,
STAR 2019 original release notice, SSE 2022 block-trade amendment, ChiNext 2020
release notice and launch-date explanation, and SZSE 2022 block-trade amendment.
Only 273,886 additional source bytes were downloaded; no failed local retrieval.
Retrieval is dated 2026-09-11, separate from applicability.

The SSE 2020 notice republishes the full rules from March 13, 2020; STAR's release
notice makes its 2019 special rules operative on publication, March 1. STAR's
article 2 retains general rules for matters not specified; article 11 inherits the
price tick and article 20 supplies its limit-order quantity interval. The 2020
deferred list concerns block trades and non-stock halt mechanisms. SSE's 2022
amendment affects block-trade pricing and main-board acceptance times from
September 5, not its August 19 publication. Its delayed 16:00–17:00 mechanism
remains unavailable. These changes do not implement block trades in this catalogue.

SZSE's 2021 primary notice explicitly takes effect April 6. The old default's
March 31 start is retained as a known legacy-migration blocker, not repaired under
its old rule id. ChiNext's 2020 notice conditions effectiveness on the first
registered IPO's listing; the August 21 exchange FAQ confirms August 24. Article
1.2 inherits unspecified general rules and 2.8 sets the limit-order maximum.
SZSE's 2022 block-trade amendment takes effect August 22, distinct from SSE's date.

The earlier composite entries use the latest reviewed amendment's effective date
as `legal_effective_from` (SSE September 5 / SZSE August 22, 2022). Each component's
own publication/effective date and order-scope impact is retained in `source_chain`.
This does not imply ordinary quantity limits changed on those amendment dates;
public resolution is clipped to the task's 2023-start calendar. The 2023 originals,
SSE repeal list, SZSE revision explanation and first-main-board-listing notices bind
the transition on April 10. A February 17 publication is not used as effectiveness.

`config/historical_rule_catalogue_v2.json` and `config/historical_rule_sources_v2.json`
must be committed and pushed before `uv run python scripts/audit_historical_rules_v2.py`.
The runner makes one start receipt, checks fixed source bytes/calendar/defaults,
caps computation and Arrow pools to two, and audits all 895 × 4 scope/dates. It also
reconciles each row with v1, retaining all previous values and listing only the
newly covered early dates. The contract retains the same 4 GiB RSS / 512 MiB output /
50 MiB additional-source / 8 GiB host-D-reserve / 45-minute heavy-work limits.

No real-data model fit, factor recomputation, provider/canonical write, return
backtest, account/order action, forward registration or promotion is authorized
in this task. Full odd-lot exits remain a conservative subset; historical stock
identity, account access, price bands/cages, special statuses, fees, corporate
actions and fills remain independent. Coverage is not performance evidence.
