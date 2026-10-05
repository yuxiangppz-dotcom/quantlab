import json
from unittest.mock import patch

from test_daily_contract import output

from quantlab.scout.demo import make_demo_market
from quantlab.scout.pipeline import DEFAULT_CONFIG, run_scout


def test_daily_pipeline_three_calls_exact_input_facts_and_immutable_reports(tmp_path, monkeypatch):
    canonical = tmp_path / "canonical"
    day = make_demo_market(canonical)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture-only")
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    prompts = []

    def ask(client, prompt, schema, search=False):
        prompts.append(prompt)
        client.calls.append({"status": "stop"})
        if len(prompts) == 1:
            result = {"hypotheses": []}
        else:
            packet = (
                json.loads(prompt.split("\n", 1)[-1])
                if len(prompts) == 2
                else json.loads(prompt[prompt.index('{"version"') :])
            )
            codes = [c["instrument_id"] for c in packet["candidates"]]
            if len(prompts) == 2:
                template = output()["comparisons"][0]["analysis"]
                result = {
                    "hypotheses": [],
                    "opportunities": [
                        {"instrument_id": code, "analysis": template} for code in codes
                    ],
                }
            else:
                rows = []
                for i, code in enumerate(codes, 1):
                    row = output()["comparisons"][1]
                    row.update(
                        instrument_id=code,
                        rank=i,
                        comparator_id=None,
                        thesis="仅当时日线的量价假设",
                        evidence_ids=["market:" + code],
                        fact_ids=[],
                    )
                    rows.append(row)
                result = {"market_view": "仅研究观察", "comparisons": rows}
        return result, {"usage": {"total_tokens": 100}, "choices": []}

    config = DEFAULT_CONFIG | {
        "provider": "deepseek",
        "daily_delivery": True,
        "opportunity_selection": True,
        "tushare_news_sources": [],
        "max_output_tokens": 32768,
        "candidate_limit": 2,
    }
    with (
        patch("quantlab.scout.pipeline.latest_completed_session", return_value=day),
        patch("quantlab.scout.ai.DeepSeekResearch.ask", new=ask),
    ):
        path, report = run_scout(
            canonical, tmp_path / "runs", config, online=True, daily_journal=tmp_path / "requests"
        )
    assert report["status"] == "live_research_unvalidated", report.get("failure")
    assert report["opportunity"]["validation"]["status"] == "complete"
    assert report["daily_delivery"]["requests"] == 3
    assert report["daily_delivery"]["charged_or_reserved_tokens"] == 300
    assert "rejected_by_code" not in report["selection_input_packet"]["market"]
    assert report["selection_input_packet"]["fact_columns"][0] == "subject_id"
    assert (path / "report.html").exists() and report["opportunity_freeze"]["status"] == "complete"
