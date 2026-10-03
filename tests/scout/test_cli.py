"""Installed CLI behavior independent of the original repository's working directory."""

import json
from datetime import datetime
from pathlib import Path

from quantlab.data.storage import ParquetStorage
from quantlab.scout.cli import main
from quantlab.scout.demo import make_demo_market
from quantlab.scout.market import inspect_market_data
from quantlab.scout.models import SHANGHAI


def test_demo_uses_current_directory_without_repository_config(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["scout", "--demo"])
    assert main() == 0
    output = json.loads(capsys.readouterr().out)
    report = Path(output["report"])
    assert report.is_relative_to(tmp_path / "data/scout/runs")
    assert report.is_file()
    assert output["status"] == "demo"
    assert not (tmp_path / "data/canonical").exists()


def test_missing_explicit_config_returns_sanitized_error(tmp_path, monkeypatch, capsys):
    missing = tmp_path / "secret-path-do-not-print.json"
    monkeypatch.setattr("sys.argv", ["scout", "--doctor", "--config", str(missing)])
    assert main() == 2
    assert "secret-path-do-not-print" not in capsys.readouterr().out


def test_doctor_reports_market_gap_without_network_or_writes(tmp_path, monkeypatch, capsys):
    canonical = tmp_path / "canonical"
    day = make_demo_market(canonical)
    monkeypatch.setattr("sys.argv", ["scout", "--doctor", "--canonical-dir", str(canonical)])
    from unittest.mock import patch

    with patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden")):
        assert main() == 0
    output = json.loads(capsys.readouterr().out)
    assert output["market_data"]["latest_21_session_window"] == day.isoformat()
    assert output["market_data"]["live_partition_files_present"] is False
    assert output["market_data"]["note"].startswith("File presence only")
    assert not (tmp_path / "data").exists()


def test_market_readiness_tracks_missing_latest_partition(tmp_path):
    canonical = tmp_path / "canonical"
    day = make_demo_market(canonical)
    storage = ParquetStorage(canonical)
    now = datetime.combine(day, datetime.min.time().replace(hour=19), SHANGHAI)
    ready = inspect_market_data(storage, now)
    assert ready["expected_session"] == day.isoformat()
    assert ready["live_partition_files_present"] is True
    storage.adj_factor_path(day).unlink()
    missing = inspect_market_data(storage, now)
    assert missing["expected_session"] == day.isoformat()
    assert missing["live_partition_files_present"] is False
    assert missing["latest_daily_and_adjustment_partition"] != day.isoformat()
