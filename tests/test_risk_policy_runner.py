import importlib.util
from datetime import date
from types import SimpleNamespace


def _load_runner():
    path = __import__("pathlib").Path(__file__).parents[1] / "scripts" / (
        "run_lifecycle_risk_policy.py"
    )
    spec = importlib.util.spec_from_file_location("risk_policy_runner", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fixed_experiment_identity_and_period() -> None:
    runner = _load_runner()
    assert runner.EXPERIMENT_SCHEMA == "lifecycle_risk_policy_v0_1"
    assert runner.PERIOD_START == date(2020, 1, 1)
    assert runner.PERIOD_END == date(2024, 12, 31)
    assert runner.LOOKBACK == 20
    assert runner.LIFECYCLE_MODE_STATUS.startswith("candidate_interpretation")


def test_blocking_event_summary_reports_unknown_coverage() -> None:
    runner = _load_runner()
    event = SimpleNamespace(
        instrument_id="002509.SZ",
        event_type="delist",
        event_date=date(2020, 7, 20),
        blocking_session=date(2020, 7, 20),
    )
    result = SimpleNamespace(
        first_blocking_event=event,
        valid_through=date(2020, 7, 17),
    )
    facts = {"002509.SZ": {"retrieval_status": "searched_unresolved", "facts": []}}
    summary = runner._blocking_event_summary(result, facts)
    assert summary["fact_coverage_status"] == "unknown"
    assert summary["retrieval_status"] == "searched_unresolved"
