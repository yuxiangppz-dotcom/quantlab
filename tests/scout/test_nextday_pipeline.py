"""Actual three-stage call interception with a 24-stock synthetic technical market."""

import json
import os
from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from test_nextday_contract import adapt_row, synthetic_history

from quantlab.data.models import AdjFactor, DailyBar, Security, TradingCalendar
from quantlab.data.storage import ParquetStorage
from quantlab.scout.ai import SYSTEM
from quantlab.scout.daily_budget import POLICY, DailyResearch
from quantlab.scout.daily_contract import (
    compact,
    compact_fact_refs,
    research_packet,
    selection_instruction,
    selection_schema,
    unpack_facts,
    validate_output,
)
from quantlab.scout.models import fingerprint
from quantlab.scout.pipeline import DEFAULT_CONFIG, run_scout


def make_synthetic_technical_market(root):
    storage = ParquetStorage(root)
    d0 = date(2026, 9, 30)
    days = sorted(
        d0 - timedelta(days=i) for i in range(180) if (d0 - timedelta(days=i)).weekday() < 5
    )[-120:]
    codes = [f"000{i:03d}.SZ" for i in range(1, 25)]
    storage.save_securities(
        [
            Security(
                c, c[:6], f"合成非真实证券{i}", "SZSE", "SZ", "主板", "L", date(2000, 1, 1), None
            )
            for i, c in enumerate(codes)
        ]
    )
    storage.save_trading_calendar(
        [TradingCalendar("SSE", day, True) for day in [*days, date(2026, 10, 8), date(2026, 10, 9)]]
    )
    for index, day in enumerate(days):
        bars = []
        for i, code in enumerate(codes):
            previous = (10 + i * 0.1) * (1.004 + i * 0.00003) ** (index - 1)
            close = (10 + i * 0.1) * (1.004 + i * 0.00003) ** index
            amount = (300_000_000 + i * 1_000_000) * (2 if index == len(days) - 1 else 1)
            bars.append(
                DailyBar(
                    code,
                    day,
                    previous,
                    close * 1.001,
                    previous * 0.999,
                    close,
                    previous,
                    amount / close,
                    amount,
                )
            )
        storage.save_daily_bars_by_date(bars, day)
        storage.save_adj_factors_by_date([AdjFactor(c, day, 1.0) for c in codes], day)
    return d0


@pytest.mark.parametrize("program_assembly", [False, True])
def test_actual_three_requests_d1_all_24_and_technical_fact_transport(
    tmp_path, monkeypatch, program_assembly
):
    data = tmp_path / "synthetic-data"
    d0 = make_synthetic_technical_market(data)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-fixture-not-a-real-key")
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    captured = []
    budget_checks = []
    original_check = DailyResearch.check_input

    def check_input(client, prompt, schema, **kwargs):
        budget_checks.append(
            {
                "chars": len(prompt),
                "prompt_bytes": len(prompt.encode()),
                "schema_bytes": len(json.dumps(schema).encode()),
                **kwargs,
            }
        )
        return original_check(client, prompt, schema, **kwargs)

    def ask(client, prompt, schema, search=False):
        captured.append((prompt, schema))
        client.calls.append({"status": "stop"})
        if len(captured) == 1:
            result = {"hypotheses": []}
        else:
            packet = json.loads(prompt[prompt.index('{"version"') :])
            rows = [
                adapt_row(c["instrument_id"], i, packet)
                for i, c in enumerate(packet["candidates"], 1)
            ]
            if len(captured) == 2:
                result = {
                    "hypotheses": [],
                    "opportunities": [
                        {"instrument_id": r["instrument_id"], "analysis": r["analysis"]}
                        for r in rows
                    ],
                }
            else:
                if program_assembly:
                    from quantlab.scout.selection_judgments import decode_model_packet, errors

                    packet = decode_model_packet(packet)

                    result = {"market_view": "合成纯量价假设，非真实预测", "comparisons": []}
                    facts = unpack_facts(packet)
                    for i, candidate in enumerate(packet["candidates"]):
                        code = candidate["instrument_id"]
                        own = next(
                            ref
                            for ref, f in facts.items()
                            if f["subject_id"] == code and f["metric"] == "close_to_ma20"
                        )
                        result["comparisons"].append(
                            {
                                "instrument_id": code,
                                "primary_type": "trend_continuation",
                                "final_status": "watch" if i < 5 else "unselected",
                                "evidence_reliability": "program_facts",
                                "comparison_strength": "weak",
                                "comparator_id": None,
                                "condition_id": code + ":price_structure_repair",
                                "support_fact_ids": [own],
                                "counter_fact_ids": [],
                                "reason": "价格位置修复可能持续，等待目标日观察",
                                "counterargument": "没有独立经营催化，可能回撤",
                                "difference": "同行比较不足，不声称优于同行",
                                "independent_basis": "纯量价假设，未验证",
                                "unknowns": "目标日需求未知",
                                "mechanism": "若套牢卖压得到消化，目标日价格才可能维持均线上方",
                            }
                        )
                    assert errors(result, packet) == []
                else:
                    result = {"market_view": "合成纯量价假设，非真实预测", "comparisons": rows}
                    assert validate_output(result, packet) == []
        return result, {"usage": {"total_tokens": 100}, "choices": []}

    config = DEFAULT_CONFIG | {
        "provider": "deepseek",
        "daily_delivery": True,
        "next_session_selection": True,
        "program_assembled_selection": program_assembly,
        "opportunity_selection": True,
        "tushare_news_sources": [],
        "tushare_industry": False,
        "tushare_disclosures": False,
        "max_output_tokens": 131072,
        "candidate_limit": 24,
    }
    with (
        patch("quantlab.scout.pipeline.latest_completed_session", return_value=d0),
        patch("quantlab.scout.ai.DeepSeekResearch.ask", new=ask),
        patch("quantlab.scout.daily_budget.DailyResearch.check_input", new=check_input),
    ):
        path, report = run_scout(
            data,
            tmp_path / "synthetic-runs",
            config,
            online=True,
            daily_journal=tmp_path / "synthetic-requests",
        )
    assert report["status"] == "live_research_unvalidated", (report.get("failure"), budget_checks)
    assert report["daily_delivery"]["requests"] == len(captured) == 3
    assert report["daily_delivery"]["repairs"] == 0
    if program_assembly:
        assert len(report["selection"]["selected"]) == 5
        assert report["selection_judgments"]["comparisons"][0]["final_status"] == "watch"
        assert report["selection_assembly"]["program_selection_score"] is None
        assert (
            report["selection_validation"]["selection_origin"] == "model_judgments_program_assembly"
        )
    for prompt, schema in captured:
        assert "[SCOUT_NEXT_SESSION_V1]" in prompt
        assert "[SCOUT_DECISION_V2]" not in prompt
        assert "scout_next_session_schema_v2" in schema["$id"]
        assert len(prompt) <= POLICY["max_input_chars"]
        assert (
            len(prompt.encode()) + len(json.dumps(schema).encode()) + len(SYSTEM.encode()) + 1000
            <= POLICY["max_input_bytes"]
        )
    assert report["selection_input_packet"]["prediction_objective"] == "next_session"
    assert len(report["nextday_freeze"]["rows"]) == 24
    assert report["nextday_freeze"]["focus_k"] == 0
    study = json.loads(captured[1][0][captured[1][0].index('{"version"') :])
    final = report["selection_input_packet"]
    for packet in (study, final):
        metrics = {value["metric"] for value in unpack_facts(packet).values()}
        if program_assembly and packet is final:
            assert {"close_location", "rsi14", "atr14_to_close", "macd_hist2_to_close"} <= metrics
            assert packet["omitted_redundant_technical_fact_counts"]["ma60"] == 24
        else:
            assert {"ma60", "close_location", "rsi14", "atr14", "macd_hist2_to_close"} <= metrics
        assert len(packet["candidates"]) == 24
        assert packet["technical_fact_indices"]
        assert '"technical_history":' not in compact(packet)
    rendered = report["opportunity"]["comparisons"]
    assert len(rendered) == 24
    if program_assembly:
        assert all(
            any(f["metric"] == "close_to_ma20" for f in row["fact_cards"]) for row in rendered
        )
    else:
        assert all("close_to_ma20" in row["technical_interpretation"] for row in rendered)
    assert all("[[" not in row["technical_interpretation"] for row in rendered)
    assert all(
        unpack_facts(final)[ref]["calculation_version"]
        for row in rendered
        for ref in row["technical_fact_ids"]
    )
    frozen_before = (path / "report.json").read_bytes()
    manifest = json.loads((path / "manifest.json").read_text())
    assert manifest["report_sha256"] == fingerprint(json.loads(frozen_before))
    assert (path / "report.html").exists()


def test_deep_baseline_budget_failure_precedes_first_model_request(tmp_path, monkeypatch):
    data = tmp_path / "synthetic-data"
    d0 = make_synthetic_technical_market(data)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-fixture-not-a-real-key")
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    config = DEFAULT_CONFIG | {
        "provider": "deepseek",
        "daily_delivery": True,
        "next_session_selection": True,
        "opportunity_selection": True,
        "tushare_news_sources": [],
        "tushare_industry": False,
        "tushare_disclosures": False,
        "max_output_tokens": 131072,
        "candidate_limit": 24,
    }
    with (
        patch("quantlab.scout.pipeline.latest_completed_session", return_value=d0),
        patch("quantlab.scout.daily_contract.selection_instruction", return_value="中" * 180001),
        patch("quantlab.scout.ai.DeepSeekResearch.ask") as transport,
    ):
        _, report = run_scout(
            data, tmp_path / "runs", config, online=True, daily_journal=tmp_path / "requests"
        )
    transport.assert_not_called()
    assert report["status"] == "incomplete"
    assert report["daily_delivery"]["requests"] == 0
    assert report["selection"]["selected"] == []
    assert not report.get("nextday_freeze")
    record = json.loads((tmp_path / "requests" / "00-budget-check-01.json").read_text())
    assert record["errors"] == ["daily_input_char_budget", "daily_input_byte_budget"]


def test_saved_real_24_input_with_explicit_synthetic_technical_pressure(tmp_path):
    source = os.environ.get("SCOUT_REAL_REPLAY_RUN")
    if not source:
        pytest.skip("Saved real report is an optional local read-only acceptance input")
    source = Path(source)
    files = {p.name: p.read_bytes() for p in source.iterdir() if p.is_file()}
    report = json.loads(files["report.json"])
    source_packet = deepcopy(report["selection_input_packet"])
    assert len(source_packet["candidates"]) == 24
    candidates = deepcopy(report["candidates"])
    for candidate in candidates:
        code = candidate["instrument_id"]
        _, _, snapshot = synthetic_history(code)
        # Pure size/contract stress. These synthetic metrics are NOT a new prediction
        # or a claim about the real companies in the unchanged saved evidence input.
        candidate["technical_snapshot"] = snapshot
        candidate.pop("program_facts", None)
    packet = research_packet(
        {
            "prediction_objective": "next_session",
            "experiment_profile": "fused",
            "candidates": candidates,
            "market": source_packet["market"],
            "timing": source_packet["timing"],
            "coverage": source_packet["coverage"],
            "evidence": source_packet["evidence"],
        }
    )
    packet["acceptance_label"] = "saved_real_input_plus_synthetic_technical_pressure_not_prediction"
    packet, _ = compact_fact_refs(packet)
    prompt, schema = (
        selection_instruction(packet) + compact(packet),
        selection_schema(packet["candidates"], packet),
    )

    class NeverCalls:
        model = "offline-stress-only"

    budget = DailyResearch(NeverCalls())
    print(
        "readonly24 pressure",
        len(prompt),
        len(prompt.encode()),
        len(json.dumps(schema).encode()),
        {k: len(compact(v)) for k, v in packet.items()},
        "fact_count",
        len(packet["facts"]),
        "unknown_unit_count",
        sum(v["unit"] == "provider_unit_unknown" for v in unpack_facts(packet).values()),
    )
    reservation = budget.check_input(prompt, schema)
    receipt = {
        "label": packet["acceptance_label"],
        "source_report_sha256": fingerprint(report),
        "candidate_count": 24,
        "prompt_chars": len(prompt),
        "prompt_bytes": len(prompt.encode()),
        "reserved_tokens": reservation,
        "model_calls": 0,
        "provider_calls": 0,
        "source_files_unchanged": all(
            (source / name).read_bytes() == content for name, content in files.items()
        ),
    }
    (tmp_path / "real-input-synthetic-technical-pressure.json").write_text(json.dumps(receipt))
    assert receipt["source_files_unchanged"]
    assert reservation <= POLICY["max_total_tokens"]
