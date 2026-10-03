"""DeepSeek V4.1 Flash uses supplied sources without fictional web search."""

import json
from datetime import datetime
from unittest.mock import patch

import pytest

from quantlab.scout.ai import DISCOVERY_SCHEMA, DeepSeekResearch
from quantlab.scout.demo import make_demo_market
from quantlab.scout.models import SHANGHAI, Coverage, Evidence
from quantlab.scout.pipeline import DEFAULT_CONFIG, read_config, run_scout


def test_deepseek_request_and_private_reasoning_redaction(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-secret-never-archive")
    raw = {
        "id": "response-1",
        "model": "deepseek-flash",
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "content": '{"hypotheses": []}',
                    "reasoning_content": "private-thinking-must-not-be-archived",
                },
            }
        ],
        "usage": {"total_tokens": 80},
    }
    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self, _):
            return json.dumps(raw).encode()

    def fetch(request, **kwargs):
        assert request.full_url == DeepSeekResearch.ENDPOINT
        requests.append(json.loads(request.data))
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fetch)
    client = DeepSeekResearch(max_output_tokens=12000, reasoning_effort="max")
    result, archived = client.ask("仅分析给定材料", DISCOVERY_SCHEMA)
    assert result == {"hypotheses": []}
    assert requests[0]["model"] == "deepseek-flash"
    assert requests[0]["reasoning_effort"] == "max"
    assert requests[0]["thinking"] == {"type": "enabled"}
    assert requests[0]["max_tokens"] == 12000
    assert requests[0]["response_format"] == {"type": "json_object"}
    assert "tools" not in requests[0]
    assert "不能联网搜索" in requests[0]["messages"][0]["content"]
    assert "private-thinking" not in json.dumps(archived)
    assert "test-secret" not in json.dumps(archived)
    with pytest.raises(ValueError, match="does not provide"):
        client.ask("search", DISCOVERY_SCHEMA, search=True)
    raw["choices"][0]["finish_reason"] = "length"
    with pytest.raises(ValueError, match="incomplete"):
        client.ask("data", DISCOVERY_SCHEMA)
    raw["choices"][0]["finish_reason"] = "stop"
    raw["choices"][0]["message"]["content"] = '{"hypotheses":[{"bad":1}]}'
    with pytest.raises(ValueError, match="schema validation"):
        client.ask("data", DISCOVERY_SCHEMA)
    assert client.calls[-1]["status"] == "schema_error"
    assert client.calls[-1]["schema_path"] == ["hypotheses", "0"]
    assert (
        client.failed_response["choices"][0]["message"]["content"] == '{"hypotheses":[{"bad":1}]}'
    )
    assert "private-thinking" not in json.dumps(client.failed_response)
    assert "test-secret" not in json.dumps(client.failed_response)


def test_deepseek_config_and_source_grounded_pipeline(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text('{"provider":"deepseek"}')
    assert read_config(config_path)["provider"] == "deepseek"
    config_path.write_text('{"provider":"deepseek","model":"unsupported"}')
    with pytest.raises(ValueError, match="deepseek-flash"):
        read_config(config_path)
    config_path.write_text('{"provider":"deepseek","deepseek_reasoning_effort":"invalid"}')
    with pytest.raises(ValueError, match="deepseek_reasoning_effort"):
        read_config(config_path)
    config_path.write_text('{"provider":"deepseek","max_output_tokens":24000}')
    assert read_config(config_path)["max_output_tokens"] == 24000
    config_path.write_text('{"provider":"openai","max_output_tokens":24000}')
    with pytest.raises(ValueError, match="max_output_tokens"):
        read_config(config_path)

    canonical = tmp_path / "canonical"
    day = make_demo_market(canonical)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-secret-never-persist")
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    config = DEFAULT_CONFIG | {"provider": "deepseek", "tushare_news_sources": []}
    stages = []

    def ask(client, prompt, schema, search=False):
        stages.append(search)
        client.calls.append({"search": search, "status": "stop", "model": client.model})
        if len(stages) < 3:
            assert "没有网页搜索" in prompt or "不得声称已联网" in prompt
            return {"hypotheses": []}, {"choices": []}
        return {"market_view": "来源不足，暂不重点观察", "selected": []}, {"choices": []}

    with (
        patch("quantlab.scout.pipeline.latest_completed_session", return_value=day),
        patch("quantlab.scout.ai.DeepSeekResearch.ask", new=ask),
        patch("quantlab.scout.pipeline.OpenAIResearch", side_effect=AssertionError("wrong AI")),
    ):
        path, report = run_scout(canonical, tmp_path / "runs", config, online=True)
    assert stages == [False, False, False]
    assert report["status"] == "live_research_unvalidated"
    assert report["ai_model"] == "deepseek-flash"
    assert report["selection"]["selected"] == []
    assert [x["status"] for x in report["coverage"] if x["source"].startswith("web_")] == [
        "not_supported",
        "not_supported",
    ]
    assert "test-secret" not in (path / "report.json").read_text()


def test_discovery_cannot_bind_a_source_omitted_from_its_prompt(tmp_path, monkeypatch):
    canonical = tmp_path / "canonical"
    day = make_demo_market(canonical)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only")
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    now = datetime.now(SHANGHAI).isoformat()
    sources = [
        Evidence(
            source="test",
            title=f"source {i}",
            body="unverified clue " * 300,
            url=f"https://example.org/{i}",
            published_at=now,
            retrieved_at=now,
        )
        for i in range(81)
    ]
    omitted_url = sources[-1].url
    stages = []

    def ask(client, prompt, schema, search=False):
        stages.append(prompt)
        client.calls.append({"search": search, "status": "stop", "model": client.model})
        if len(stages) == 1:
            assert omitted_url not in prompt
            return {
                "hypotheses": [
                    {
                        "summary": "unsupported",
                        "instrument_ids": ["600001.SH"],
                        "relation": "theme",
                        "source_urls": [omitted_url],
                        "counterargument": "unknown",
                    }
                ]
            }, {}
        if len(stages) == 2:
            return {"hypotheses": []}, {}
        return {"market_view": "no verified thesis", "selected": []}, {}

    config = DEFAULT_CONFIG | {"provider": "deepseek", "tushare_news_sources": []}
    with (
        patch("quantlab.scout.pipeline.latest_completed_session", return_value=day),
        patch(
            "quantlab.scout.pipeline.collect_sources",
            return_value=(sources, [Coverage("test", "ok")]),
        ),
        patch("quantlab.scout.ai.DeepSeekResearch.ask", new=ask),
    ):
        _, report = run_scout(canonical, tmp_path / "runs", config, online=True)
    assert len(stages) == 3
    assert len(stages[0]) < 25_000
    assert len(stages[1]) < 45_000
    assert report["status"] == "live_research_unvalidated"
    assert report["hypotheses"] == []
    assert report["prompt_evidence_audit"][0]["stage"] == "discovery"
    assert report["prompt_evidence_audit"][0]["body_truncated_count"] > 0
