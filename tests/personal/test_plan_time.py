from datetime import date

import pytest
from test_plan import HEADER, _seed_product

from quantlab.data.models import TradingCalendar
from quantlab.personal import import_manual_cash_flows
from quantlab.personal.account import import_account_csv
from quantlab.personal.plan import build_reference_plan

FLOW_HEADER = "account_id,external_flow_id,effective_at,reported_at,direction,amount_cny\n"


def _seed(tmp_path, opening="2026-09-09T08:00:00+08:00"):
    product_root, storage = _seed_product(tmp_path)
    storage.save_trading_calendar(
        [TradingCalendar("SZSE", date(2026, 9, day), True) for day in (9, 10, 11)]
    )
    account_root = tmp_path / "accounts"
    import_account_csv(
        (HEADER + f"mine,manual_tracking,{opening},100000,,0,0,,none_declared\n").encode(),
        account_root=account_root,
    )
    return {"product_root": product_root, "storage": storage, "account_root": account_root}


@pytest.mark.parametrize("direction", ["DEPOSIT", "WITHDRAWAL"])
@pytest.mark.parametrize(
    "effective",
    [
        "2026-09-10T00:00:00+08:00",
        "2026-09-09T16:00:00+00:00",
    ],
)
def test_late_cash_flow_cannot_drive_prior_plan_or_write_artifacts(tmp_path, direction, effective):
    context = _seed(tmp_path)
    raw = (FLOW_HEADER + f"mine,F1,{effective},{effective},{direction},500\n").encode()
    import_manual_cash_flows("mine", raw, **context)
    with pytest.raises(ValueError, match="stale relative to imported cash flows"):
        build_reference_plan("mine", **context)
    assert not list(context["account_root"].glob("mine/plans/**/plan.*"))


@pytest.mark.parametrize("direction,expected", [("DEPOSIT", 10050000), ("WITHDRAWAL", 9950000)])
def test_late_report_of_earlier_economic_flow_does_not_advance_cutoff(
    tmp_path, direction, expected
):
    context = _seed(tmp_path)
    raw = (
        FLOW_HEADER + "mine,F1,2026-09-09T15:00:00+08:00,"
        f"2026-09-11T17:00:00+08:00,{direction},500\n"
    ).encode()
    import_manual_cash_flows("mine", raw, **context)
    _, _, plan = build_reference_plan("mine", **context)
    assert plan["planning_nav_fen"] == expected
    assert plan["intended_next_session"] == "2026-09-10"
    assert not plan["execution_confirmed"]


@pytest.mark.parametrize(
    "opening,blocked",
    [
        ("2026-09-10T08:00:00+08:00", False),
        ("2026-09-10T16:00:00+00:00", True),
    ],
)
def test_opening_basis_must_not_postdate_intended_session(tmp_path, opening, blocked):
    context = _seed(tmp_path, opening=opening)
    if blocked:
        with pytest.raises(ValueError, match="account basis is later than intended session"):
            build_reference_plan("mine", **context)
        assert not list(context["account_root"].glob("mine/plans/**/plan.*"))
    else:
        _, _, plan = build_reference_plan("mine", **context)
        assert plan["planning_nav_fen"] == 10000000
