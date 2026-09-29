"""Installed CLI behavior independent of the original repository's working directory."""

import json
from pathlib import Path

from quantlab.scout.cli import main


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
