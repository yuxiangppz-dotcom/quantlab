"""High-risk TuShare normalization and recall boundaries."""

from datetime import date, datetime
from pathlib import Path

import pandas as pd

from quantlab.scout.market import add_sectors
from quantlab.scout.models import SHANGHAI, Candidate
from quantlab.scout.pipeline import candidate_source_summary
from quantlab.scout.tushare_upgrade import (
    TusharePack,
    collect_deep_pack,
    collect_market_pack,
    collect_sw_memberships,
)

NOW = datetime(2026, 10, 1, 12, tzinfo=SHANGHAI)
SESSION = date(2026, 9, 30)
CODE = "600001.SH"
EXPANSION = "600002.SH"


def test_sector_route_includes_breadth_beyond_two_price_leaders():
    universe = {
        f"60000{rank}.SH": Candidate(
            f"60000{rank}.SH", f"示例{rank}", {"return_1d": 0.02}, float(rank)
        )
        for rank in range(1, 6)
    }
    selected = add_sectors(universe, {code: "示例行业" for code in universe}, quota=3)
    assert selected == ["600005.SH", "600003.SH", "600001.SH"]
    assert all("板块关联:示例行业" in universe[code].routes for code in selected)


def test_source_summary_uses_each_candidate_own_industry():
    memberships = {CODE: "电力设备", EXPANSION: "房地产"}
    for code in memberships:
        candidate = {
            "instrument_id": code,
            "context": {"tushare_upgrade": {"moneyflow": {"coverage_1d": 1}}},
        }
        assert candidate_source_summary(candidate, memberships)["industry"] == memberships[code]


def test_market_pack_uses_new_event_and_theme_without_price_requirement(tmp_path, monkeypatch):
    pack = TusharePack(tmp_path, NOW, False)

    def fetch(api, params):
        if api == "forecast_vip" and params.get("period") == "20260630":
            return [
                {
                    "ts_code": EXPANSION,
                    "ann_date": "20260930",
                    "end_date": "20260630",
                    "type": "预增",
                    "net_profit_min": 100.0,
                },
                {
                    "ts_code": CODE,
                    "ann_date": "20261002",
                    "end_date": "20260630",
                    "type": "预增",
                    "net_profit_min": 100.0,
                },
            ]
        if api == "kpl_list":
            return [{"ts_code": CODE, "trade_date": "20260930", "tag": "涨停"}]
        if api == "kpl_concept_cons" and "con_code" in params:
            return [{"ts_code": "000111.KP"}]
        if api == "kpl_concept_cons" and "ts_code" in params:
            return [
                {
                    "ts_code": "000111.KP",
                    "name": "示例题材",
                    "con_code": EXPANSION,
                    "con_name": "测试乙",
                    "trade_date": "20260930",
                }
            ]
        if api == "share_float":
            return [
                {
                    "ts_code": CODE,
                    "ann_date": "20260929",
                    "float_date": "20261005",
                    "float_ratio": 2.0,
                    "float_share": 1000,
                }
            ]
        if api == "moneyflow" and params["trade_date"] == "20260930":
            return [{"ts_code": CODE, "trade_date": "20260930", "net_mf_amount": -20.0}]
        return []

    monkeypatch.setattr(pack, "fetch", fetch)
    days = [date(2026, 9, day) for day in (24, 25, 28, 29, 30)]
    evidence, leads, context = collect_market_pack(
        pack, SESSION, days, {CODE, EXPANSION}, {CODE: "测试甲", EXPANSION: "测试乙"}
    )
    assert any(x["instrument_ids"] == [EXPANSION] and "event" in x["relation"] for x in leads)
    assert any(x["instrument_ids"] == [EXPANSION] and "theme" in x["relation"] for x in leads)
    assert not any(x.event_dates == ("2026-10-02",) for x in evidence)
    assert any(x.kind == "known_future_unlock" for x in evidence)
    assert context[CODE]["moneyflow"]["net_1d_wan_cny"] == -20.0
    assert context[CODE]["moneyflow"]["net_5d_wan_cny"] is None
    assert context[CODE]["moneyflow"]["coverage_5d"] == 1


def test_current_sw_membership_and_immutable_cache(tmp_path):
    pack = TusharePack(tmp_path, NOW, False)

    class API:
        calls = 0

        def index_classify(self, **params):
            self.calls += 1
            return pd.DataFrame([{"index_code": "801010.SI", "industry_name": "农业"}])

        def index_member_all(self, **params):
            self.calls += 1
            return pd.DataFrame(
                [
                    {"ts_code": CODE, "l1_name": "农业", "is_new": "Y"},
                    {"ts_code": EXPANSION, "l1_name": "农业", "is_new": "N"},
                ]
            )

    api = API()
    pack.client = api
    assert collect_sw_memberships(pack, {CODE, EXPANSION}) == {CODE: "农业"}
    assert api.calls == 2
    assert collect_sw_memberships(pack, {CODE, EXPANSION}) == {CODE: "农业"}
    assert api.calls == 2
    assert len(list(tmp_path.glob("*.json"))) == 2
    member = pack.fetch("index_member_all", {"l1_code": "801010.SI", "is_new": "Y"})[0]
    reference = pack.evidence(
        "index_member_all", member, CODE, "申万分类", "industry", ("ts_code",)
    )
    assert len(reference.snapshot_refs) == 1
    assert Path(reference.snapshot_refs[0]).exists()


def test_capped_unlock_window_is_partitioned_without_losing_future_event(tmp_path, monkeypatch):
    pack = TusharePack(tmp_path, NOW, False)
    requests = []

    def fetch(api, params):
        if api != "share_float":
            return []
        requests.append(params)
        if params == {"start_date": "20261002", "end_date": "20261008"}:
            return [{}] * 6000
        if params == {"start_date": "20261002", "end_date": "20261005"}:
            return [
                {
                    "ts_code": CODE,
                    "ann_date": "20260930",
                    "float_date": "20261003",
                    "float_ratio": 1.0,
                }
            ]
        return []

    monkeypatch.setattr(pack, "fetch", fetch)
    evidence, _, _ = collect_market_pack(pack, SESSION, [], {CODE}, {CODE: "测试甲"})
    assert {"start_date": "20261006", "end_date": "20261008"} in requests
    assert any(item.kind == "known_future_unlock" for item in evidence)


def test_main_business_segments_have_one_evidence_and_keep_unit_warning(tmp_path, monkeypatch):
    pack = TusharePack(tmp_path, NOW, False)

    def fetch(api, params):
        if api == "fina_mainbz":
            return [
                {"end_date": "20260630", "bz_item": "境内", "bz_sales": 10, "curr_type": "CNY"},
                {"end_date": "20260630", "bz_item": "境外", "bz_sales": 20, "curr_type": "CNY"},
            ]
        return []

    monkeypatch.setattr(pack, "fetch", fetch)
    evidence, context = collect_deep_pack(pack, [{"instrument_id": CODE}], [])
    assert len(evidence) == 1
    assert len(context[CODE]["fina_mainbz"]) == 2
    assert "dimensions cannot be summed" in evidence[0].body


def test_deep_unlock_is_stock_scoped_and_future_publication_is_excluded(tmp_path, monkeypatch):
    pack = TusharePack(tmp_path, NOW, False)
    requested = []

    def fetch(api, params):
        if api != "share_float":
            return []
        requested.append(params)
        return [
            {"ts_code": CODE, "ann_date": "20260930", "float_date": "20261009"},
            {"ts_code": CODE, "ann_date": "20261002", "float_date": "20261009"},
            {"ts_code": EXPANSION, "ann_date": "20260930", "float_date": "20261009"},
        ]

    monkeypatch.setattr(pack, "fetch", fetch)
    evidence, context = collect_deep_pack(pack, [{"instrument_id": CODE}], [])
    assert requested[0]["ts_code"] == CODE
    assert [item.kind for item in evidence] == ["known_future_unlock"]
    assert len(context[CODE]["future_unlocks"]) == 1
