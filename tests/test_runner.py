import importlib.util
import sys
from datetime import date, timedelta
from pathlib import Path

_RUNNER_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run_alpha_research.py"


def _load_runner():
    spec = importlib.util.spec_from_file_location("run_alpha_research", _RUNNER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_alpha_research"] = module
    spec.loader.exec_module(module)
    return module


def test_momentum_20d_config_fixed() -> None:
    runner = _load_runner()
    cfg = runner._EXPERIMENTS["momentum_20d"]
    assert cfg["experiment_name"] == "momentum_20d"
    assert cfg["universe"] == "V1 SH/SZ A-share"
    assert cfg["alpha_definition"] == "return_20d"
    assert cfg["lookback"] == 20
    assert cfg["label_horizons"] == [5, 20]
    assert cfg["discovery"] == ["2010-01-04", "2019-12-31"]
    assert cfg["validation"] == ["2020-01-01", "2024-12-31"]
    assert cfg["test"] == ["2025-01-01", "2026-09-04"]
    assert cfg["test_observed"] is True


def test_metadata_policies() -> None:
    runner = _load_runner()
    cfg = runner._EXPERIMENTS["momentum_20d"]
    assert cfg["label_period_policy"] == "target_session_must_be_within_period"
    assert cfg["quantile_tie_policy"] == "average_rank_keep_ties"


def test_eligible_signal_end_20d() -> None:
    runner = _load_runner()
    dates = [date(2019, 12, 1) + timedelta(days=i) for i in range(31)]
    eligible = runner._eligible_signal_end(dates, date(2019, 12, 31), 20)
    assert eligible == dates[10]  # last_idx(30) - 20 = 10


def test_eligible_signal_end_5d_later_than_20d() -> None:
    runner = _load_runner()
    dates = [date(2019, 12, 1) + timedelta(days=i) for i in range(31)]
    e5 = runner._eligible_signal_end(dates, date(2019, 12, 31), 5)
    e20 = runner._eligible_signal_end(dates, date(2019, 12, 31), 20)
    assert e5 > e20


def test_eligible_signal_end_validation_2024() -> None:
    runner = _load_runner()
    dates = [date(2024, 12, 1) + timedelta(days=i) for i in range(31)]
    eligible = runner._eligible_signal_end(dates, date(2024, 12, 31), 20)
    assert eligible == dates[10]
    assert eligible <= date(2024, 12, 31)


def test_eligible_signal_end_insufficient() -> None:
    runner = _load_runner()
    dates = [date(2026, 1, 5) + timedelta(days=i) for i in range(5)]
    assert runner._eligible_signal_end(dates, date(2026, 1, 9), 20) is None


def test_runner_project_root_cwd_independent(tmp_path, monkeypatch) -> None:
    runner = _load_runner()
    root = runner.PROJECT_ROOT
    assert root.is_absolute()
    monkeypatch.chdir(tmp_path)
    assert runner.PROJECT_ROOT == root
    canonical_path = root / "data" / "canonical"
    assert canonical_path.is_absolute()


def test_git_sha_returns_str_or_none() -> None:
    runner = _load_runner()
    sha = runner._git_sha()
    assert sha is None or isinstance(sha, str)
