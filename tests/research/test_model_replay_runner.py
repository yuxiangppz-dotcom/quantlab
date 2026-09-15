"""Exercise the actual day orchestrator on independently hand-priced tiny worlds."""

import importlib.util
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pandas as pd

from quantlab.research.quantity_kernel import (
    ResearchFeeScenario,
    ResearchQuantityRules,
    ResearchSession,
)


def runner_module():
    path = Path(__file__).resolve().parents[2] / "scripts/replay_alpha158_model.py"
    spec = importlib.util.spec_from_file_location("model_replay_runner_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class World:
    suspend = False
    missing = False

    def __init__(self, root):
        self.calendar = [date(2023, 1, x) for x in (3, 4, 5, 6)]
        self.manifest, self.securities, self.unplaced, self.changes = {}, {}, {}, []

    def bind(self, path):
        return None

    def targets(self, exclude_st=False):
        return pd.DataFrame(
            {
                "instrument_id": ["A"] * 3,
                "trade_date": pd.to_datetime(self.calendar[:3]),
                "rank": [1] * 3,
            }
        )

    def load_events(self, *args):
        pass

    def security(self, *args):
        return ("SSE", "MAIN")

    def distributions(self, *args):
        return []

    def suspended(self, code, day):
        return self.suspend and day == self.calendar[2]

    def partition(self, kind, day):
        if day == self.calendar[2] and (self.suspend or self.missing):
            return {}
        return {"A": {"close": 10}}

    def context(self, code, day, signal):
        return ResearchSession(
            code,
            day,
            self.calendar[self.calendar.index(day) + 1],
            day,
            True,
            not self.suspended(code, day),
            True,
            1000,
            900,
            1100,
            800,
            1200,
            100000000,
            signal,
            20,
            100000000,
            100000,
            Decimal(".05"),
            ResearchQuantityRules("test", signal, day, 100, 100, 100, 100, 1000000, True),
            ResearchFeeScenario(
                "test",
                signal,
                day,
                Decimal(".000086"),
                500,
                Decimal(0),
                Decimal(".001"),
                Decimal(0),
                0,
                Decimal(".0005"),
            ),
        )


def test_complete_runner_cash_shares_and_net_return(tmp_path, monkeypatch):
    module = runner_module()
    monkeypatch.setattr(module, "ReplayData", World)
    result = module.run(tmp_path, tmp_path / "out")
    assert result["status"] == "completed_scenario"
    # First buy: 1000 shares * adverse 1001 fen + minimum commission 500 fen.
    day = json.loads((tmp_path / "out/days/2023-01-04.json").read_text())
    assert day["book"]["cash_fen"] == 18998500
    assert day["record"]["equity_fen"] == 19998500
    assert sum(lot["quantity"] for lot in day["book"]["lots"]) == 1000
    assert result["metrics"]["total_return"] == (19998500 / 20000000 - 1)


def test_suspension_valuation_is_separate_from_execution(tmp_path, monkeypatch):
    class Suspended(World):
        suspend = True

    module = runner_module()
    monkeypatch.setattr(module, "ReplayData", Suspended)
    result = module.run(tmp_path, tmp_path / "out")
    assert result["status"] == "completed_scenario"
    day = json.loads((tmp_path / "out/days/2023-01-05.json").read_text())
    assert day["stale_marks"][0]["observed_date"] == "2023-01-04"
    assert all(a["quantity"] == 0 for a in day["attempts"])


def test_unknown_mark_keeps_last_complete_day(tmp_path, monkeypatch):
    class Missing(World):
        missing = True

    module = runner_module()
    monkeypatch.setattr(module, "ReplayData", Missing)
    result = module.run(tmp_path, tmp_path / "out")
    assert result["status"] == "stopped_incomplete_scenario"
    assert result["metrics"] is None
    assert result["committed_days"] == 2
    assert not (tmp_path / "out/days/2023-01-05.json").exists()


def test_independent_verifier_accepts_hand_world_and_rejects_fee_tamper(tmp_path, monkeypatch):
    import pytest

    module = runner_module()
    monkeypatch.setattr(module, "ReplayData", World)
    output = tmp_path / "out"
    module.run(tmp_path, output)
    for day in ("2023-01-03", "2023-01-04", "2023-01-05"):
        path = tmp_path / "canonical/daily/year=2023/month=01" / (day + ".parquet")
        path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame({"instrument_id": ["A"], "close": [10]}).to_parquet(path)
    file = Path(__file__).resolve().parents[2] / "scripts/verify_model_replay.py"
    spec = importlib.util.spec_from_file_location("independent_model_replay_test", file)
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    assert verifier.verify(output, tmp_path / "canonical")["all_ok"]
    path = output / "days/2023-01-04.json"
    day = json.loads(path.read_text())
    day["attempts"][0]["commission_fen"] = 0
    path.write_text(json.dumps(day))
    with pytest.raises(AssertionError):
        verifier.verify(output, tmp_path / "canonical")
