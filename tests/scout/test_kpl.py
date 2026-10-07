"""Third-party limit-up themes never become official company facts."""

from datetime import date, datetime
from unittest.mock import Mock, patch

import pandas as pd

from quantlab.scout.models import SHANGHAI
from quantlab.scout.pipeline import DEFAULT_CONFIG
from quantlab.scout.sources import collect_kpl_limit_reasons

SESSION = date(2026, 9, 29)
CODE = "002813.SZ"
CONFIG = DEFAULT_CONFIG | {"tushare_kpl_limit": True}


def test_kpl_disabled_or_before_documented_update_does_not_call_provider(monkeypatch):
    monkeypatch.setenv("TUSHARE_TOKEN", "test-token")
    early = datetime(2026, 9, 30, 5, 59, tzinfo=SHANGHAI)
    with patch("tushare.pro_api") as provider:
        assert (
            collect_kpl_limit_reasons(DEFAULT_CONFIG, early, True, SESSION, [CODE])[1].status
            == "not_configured"
        )
        assert (
            collect_kpl_limit_reasons(CONFIG, early, True, SESSION, [CODE])[1].status
            == "not_yet_expected"
        )
    provider.assert_not_called()


def test_kpl_theme_has_unknown_publication_time_and_is_targeted(monkeypatch):
    monkeypatch.setenv("TUSHARE_TOKEN", "test-token")
    provider = Mock()
    provider.kpl_list.return_value = pd.DataFrame(
        [
            {
                "ts_code": CODE,
                "trade_date": "20260929",
                "tag": "涨停",
                "theme": "无人驾驶、智能座舱",
                "status": "首板",
                "lu_desc": "无人驾驶",
            },
            {
                "ts_code": "000001.SZ",
                "trade_date": "20260929",
                "tag": "涨停",
                "theme": "银行",
                "status": "首板",
                "lu_desc": "银行",
            },
            {
                "ts_code": CODE,
                "trade_date": "20260928",
                "tag": "涨停",
                "theme": "错日",
                "status": "首板",
                "lu_desc": "错日",
            },
        ]
    )
    with patch("tushare.pro_api", return_value=provider):
        evidence, coverage = collect_kpl_limit_reasons(
            CONFIG, datetime(2026, 9, 30, 7, tzinfo=SHANGHAI), True, SESSION, [CODE]
        )
    assert len(evidence) == 1
    assert evidence[0].instrument_ids == (CODE,)
    assert evidence[0].published_at is None
    assert evidence[0].event_dates == ("2026-09-29",)
    assert evidence[0].url is None
    assert evidence[0].kind == "theme_board_unverified"
    assert "不是上市公司公告" in evidence[0].body
    assert coverage.status == "partial"
    assert coverage.count == 1
    provider.kpl_list.assert_called_once()


def test_kpl_permission_failure_is_coverage(monkeypatch):
    monkeypatch.setenv("TUSHARE_TOKEN", "test-token")
    provider = Mock()
    provider.kpl_list.side_effect = RuntimeError("provider detail must not be persisted")
    with patch("tushare.pro_api", return_value=provider):
        evidence, coverage = collect_kpl_limit_reasons(
            CONFIG, datetime(2026, 9, 30, 7, tzinfo=SHANGHAI), True, SESSION, [CODE]
        )
    assert evidence == []
    assert coverage.status == "failed"
    assert coverage.detail == "RuntimeError"
