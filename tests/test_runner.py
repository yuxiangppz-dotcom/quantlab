import importlib.util
import sys
from pathlib import Path


def _load_runner():
    path = Path("scripts/run_alpha_research.py").resolve()
    spec = importlib.util.spec_from_file_location("run_alpha_research", path)
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


def test_runner_functions_exist() -> None:
    runner = _load_runner()
    assert callable(runner.run_momentum_20d)
    assert callable(runner._git_sha)
    assert callable(runner._run_year)
    assert callable(runner._period_metrics)
    assert callable(runner._print_period)


def test_git_sha_returns_str_or_none() -> None:
    runner = _load_runner()
    sha = runner._git_sha()
    assert sha is None or isinstance(sha, str)
