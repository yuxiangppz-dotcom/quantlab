"""GLM-5.3 adapter request, source handling, and pipeline selection."""

import json
from unittest.mock import patch

import pytest

from quantlab.scout.ai import (
    DISCOVERY_SCHEMA,
    ZAIResearch,
    bind_hypotheses,
    search_evidence,
)
from quantlab.scout.demo import make_demo_market
from quantlab.scout.models import SHANGHAI
from quantlab.scout.pipeline import DEFAULT_CONFIG, read_config, run_scout


def test_glm_request_is_bounded_and_archives_no_reasoning(monkeypatch):
    monkeypatch.setenv("ZAI_API_KEY", "test-secret-not-to-archive")
    raw = {
        "id": "test-response",
        "model": "glm-5.3",
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "content": json.dumps(
                        {
                            "hypotheses": [
                                {
                                    "summary": "待核实关系",
                                    "instrument_ids": ["600001.SH"],
                                    "relation": "theme",
                                    "source_urls": ["https://example.org/notice"],
                                    "counterargument": "公告未说明直接供货",
                                }
                            ]
                        }
                    ),
                    "reasoning_content": "private-thinking-must-not-be-archived",
                },
            }
        ],
        "usage": {"total_tokens": 100},
        "web_search": [
            {
                "title": "公告搜索结果",
                "content": "搜索结果摘要",
                "link": "https://example.org/notice",
                "publish_date": "2026-01-09",
            }
        ],
    }
    payloads = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self, _):
            return json.dumps(raw).encode()

    def fetch(request, **kwargs):
        assert request.full_url == ZAIResearch.ENDPOINT
        payloads.append(json.loads(request.data))
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fetch)
    client = ZAIResearch("glm-5.3", max_output_tokens=6000, max_tool_calls=5)
    result, archived = client.ask("调查候选", DISCOVERY_SCHEMA, search=True)
    assert result["hypotheses"][0]["relation"] == "theme"
    assert payloads[0]["thinking"] == {"type": "enabled"}
    assert payloads[0]["reasoning_effort"] == "max"
    assert payloads[0]["max_tokens"] == 6000
    assert payloads[0]["response_format"] == {"type": "json_object"}
    assert payloads[0]["tools"][0]["web_search"]["count"] == 5
    assert "test-secret" not in json.dumps(archived)
    assert "private-thinking" not in json.dumps(archived)
    from datetime import datetime

    found = search_evidence(archived, datetime(2026, 1, 10, tzinfo=SHANGHAI))
    assert len(found) == 1
    assert found[0].source == "zai_web_search"
    assert found[0].published_at is None
    assert found[0].kind == "search_snippet_unverified"
    bound = bind_hypotheses(result, found, {"600001.SH"})
    assert bound[0]["evidence_ids"] == [found[0].evidence_id]


def test_glm_refuses_missing_sources_and_partial_completion(monkeypatch):
    monkeypatch.setenv("ZAI_API_KEY", "test-only")

    class Response:
        def __init__(self, raw):
            self.raw = raw

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self, _):
            return json.dumps(self.raw).encode()

    raw = {
        "choices": [{"finish_reason": "stop", "message": {"content": '{"hypotheses": []}'}}],
        "web_search": [],
    }
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: Response(raw))
    client = ZAIResearch()
    with pytest.raises(ValueError, match="no source"):
        client.ask("search", DISCOVERY_SCHEMA, search=True)
    raw["choices"][0]["finish_reason"] = "length"
    with pytest.raises(ValueError, match="incomplete"):
        client.ask("search", DISCOVERY_SCHEMA, search=False)


def test_glm_configuration_and_mock_live_run(tmp_path, monkeypatch):
    config_path = tmp_path / "scout.json"
    config_path.write_text('{"provider":"zai"}')
    config = read_config(config_path)
    assert config["provider"] == "zai"
    for invalid in ('{"provider":"unknown"}', '{"provider":[]}'):
        config_path.write_text(invalid)
        with pytest.raises(ValueError, match="provider"):
            read_config(config_path)
    config_path.write_text('{"provider":"zai","model":"other"}')
    with pytest.raises(ValueError, match="glm-5.3"):
        read_config(config_path)

    canonical = tmp_path / "canonical"
    day = make_demo_market(canonical)
    monkeypatch.setenv("ZAI_API_KEY", "test-secret-never-persist")
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    config = DEFAULT_CONFIG | {"provider": "zai", "tushare_news_sources": []}

    def ask(client, prompt, schema, search=False):
        client.calls.append({"search": search, "status": "stop", "model": client.model})
        if search:
            return {"hypotheses": []}, {
                "web_search": [{"title": "test", "link": "https://example.org/notice"}]
            }
        return {"market_view": "证据不足，继续观察", "selected": []}, {"status": "completed"}

    with (
        patch("quantlab.scout.pipeline.latest_completed_session", return_value=day),
        patch("quantlab.scout.ai.ZAIResearch.ask", new=ask),
    ):
        path, report = run_scout(canonical, tmp_path / "runs", config, online=True)
    assert report["status"] == "live_research_unvalidated"
    assert report["config"]["provider"] == "zai"
    assert report["ai_provider"] == "zai"
    assert report["ai_model"] == "glm-5.3"
    assert len(report["ai_calls"]) == 3
    assert "test-secret" not in (path / "report.json").read_text()
    assert "zai / glm-5.3" in (path / "audit_report.md").read_text()
