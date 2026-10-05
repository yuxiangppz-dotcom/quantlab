from copy import deepcopy

import pytest
from test_daily_contract import inputs, output

from quantlab.scout.daily_contract import render_output, unpack_facts, validate_output
from quantlab.scout.daily_semantics import BENCHMARK, claim_errors, render_claim
from quantlab.scout.html_report import render_html_report
from quantlab.scout.report import present_selection, render_report


def facts():
    return {
        str(i): {
            "subject_id": "a",
            "metric": metric,
            "period": period,
            "value": str(value),
            "unit": unit,
        }
        for i, (metric, period, value, unit) in enumerate(
            [
                ("return_5d", "5d", 0.3, "ratio"),
                ("relative_return_5d", "5d", -0.1, "ratio"),
                ("net_1d_wan_cny", "1d", 1, "wan_CNY"),
                ("net_3d_wan_cny", "3d", -1, "wan_CNY"),
                ("net_5d_wan_cny", "5d", 2, "wan_CNY"),
                ("amount_ratio_5d", "5d", 2, "times"),
            ]
        )
    }


def claim(kind, ids, periods, direction="positive", benchmark=None):
    return dict(
        kind=kind,
        subject_id="a",
        fact_ids=ids,
        periods=periods,
        direction=direction,
        benchmark=benchmark,
    )


@pytest.mark.parametrize(
    "item",
    [
        claim("relative_return", ["0"], ["5d"], benchmark=BENCHMARK),
        claim("relative_return", ["1"], ["5d"], benchmark=BENCHMARK),
        claim("fund_windows_same_sign", ["2"], ["1d"]),
        claim("fund_windows_same_sign", ["2", "3"], ["1d", "3d"]),
        claim("fund_windows_same_sign", ["2", "missing"], ["1d", "5d"]),
        claim("amount_multiple", ["5"], ["1d"]),
        claim("relative_return", ["1"], ["5d"], "negative"),
    ],
)
def test_declared_kind_subject_window_direction_unit_and_benchmark_are_checked(item):
    assert claim_errors([item], facts(), {"a"})
    item = deepcopy(item)
    item["subject_id"] = "b"
    assert claim_errors([item], facts(), {"a", "b"})


def test_valid_overlapping_windows_are_not_improvement_or_independent_evidence():
    item = claim("fund_windows_same_sign", ["2", "4"], ["1d", "5d"])
    assert claim_errors([item], facts(), {"a"}) == []
    rendered = render_claim(item, facts(), lambda f: f["metric"])
    assert "重叠" in rendered and "不能证明逐日改善" in rendered


@pytest.mark.parametrize(
    "text",
    [
        "多日相对收益走强：[[fact:000001.SZ:market:return_5d]]",
        "资金窗口一致：[[fact:000001.SZ:market:return_1d]]",
        "量比放大：[[fact:000001.SZ:market:return_5d]]",
        "超越行业：[[fact:000001.SZ:market:return_5d]]",
        "持续资金改善；资金持续改善",
    ],
)
def test_free_prose_cannot_relabel_core_facts_even_with_synonyms(text):
    packet, result = inputs(), output()
    result["comparisons"][0]["thesis"] = text
    assert validate_output(result, packet)


def test_program_limit_and_extension_risks_survive_all_concise_views():
    from test_concise_report import report

    saved = report()
    for row in saved["selection"]["selected"]:
        row["evidence_ids"] = []
    candidates = [
        {**c, "metrics": {"close": 10, "up_limit": 10, "return_5d": 0.3}}
        for c in saved["candidates"]
    ]
    saved["selection"] = present_selection(saved["selection"] | {"market_view": "观察"}, candidates)
    for view in (render_report(saved), render_html_report(saved)):
        assert "收盘等于涨停价" in view and "近期涨幅较大" in view
        assert "实际可买性仍未知" in view and "原模型反证" in view


def test_real_reference_format_keeps_relative_benchmark_and_amount_name():
    from quantlab.scout.daily_contract import format_fact

    relative = facts()["1"]
    assert "本次合格行业成员均值" in format_fact(relative)
    packet, result = inputs(), output()
    before = deepcopy(result)
    assert validate_output(result, packet) == []
    assert render_output(result, packet)["raw_output_sha256"]
    assert result == before and unpack_facts(packet)
