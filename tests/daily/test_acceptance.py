from pathlib import Path

import pandas as pd

from quantlab.daily import acceptance
from quantlab.daily.service import DailySnapshot


def test_acceptance_requires_and_publishes_the_real_user_path(tmp_path: Path, monkeypatch) -> None:
    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    ranking = daily_dir / "ranking.csv"
    pd.DataFrame(
        [
            {
                "instrument_id": "000001.SZ",
                "alpha_score": 1,
                "selected": True,
                "target_weight": 0.05,
                "risk_context": "unknown",
            }
        ]
    ).to_csv(ranking, index=False)
    target = daily_dir / "target_portfolio.csv"
    target.write_text("instrument_id\n")
    report_path = daily_dir / "report.json"
    report_path.write_text("{}")
    html = daily_dir / "report.html"
    html.write_text("ok")
    snapshot = DailySnapshot(
        report_path,
        ranking,
        target,
        html,
        {
            "effective_as_of": "2026-09-09",
            "model": {"config_id": "daily_mvp_v1", "model_status": "baseline"},
            "content_fingerprint": "daily-sha",
        },
        True,
    )
    monkeypatch.setattr(
        acceptance, "inspect_data_status", lambda: {"effective_as_of": "2026-09-09"}
    )
    monkeypatch.setattr(acceptance, "load_validated_latest_snapshot", lambda: snapshot)
    monkeypatch.setattr(
        acceptance,
        "load_baseline_view",
        lambda: {"schema": "formal", "run_id": "run"},
    )
    monkeypatch.setattr(
        acceptance,
        "load_latest_factor_view",
        lambda: {"run_id": "factor-run", "signal_end": "2026-09-02"},
    )
    monkeypatch.setattr(acceptance, "list_accounts", lambda: ["demo_200k"])
    monkeypatch.setattr(
        acceptance,
        "load_effective_account",
        lambda account_id: {"account_fingerprint": "account-sha"},
    )
    monkeypatch.setattr(acceptance, "manual_tracking_fixture_smoke", lambda: True)
    monkeypatch.setattr(
        acceptance,
        "load_latest_plan",
        lambda account_id, account_fingerprint=None: (
            tmp_path / "plan.json",
            {
                "plan_id": "plan",
                "account_fingerprint": "account-sha",
                "daily_content_fingerprint": "daily-sha",
                "csv_sha256": "csv-sha",
            },
        ),
    )
    monkeypatch.setattr(
        acceptance,
        "_git",
        lambda command: "" if command == "status --porcelain" else "a" * 40,
    )
    json_path, html_path, payload = acceptance.run_v1_acceptance(
        output_root=tmp_path / "acceptance"
    )
    assert payload["product_ready"] is True
    assert payload["investment_strategy_promoted"] is False
    assert payload["external_order_submission"] is False
    assert json_path.exists() and html_path.exists()

    # A new effective account fingerprint must not reuse the old plan.
    monkeypatch.setattr(
        acceptance,
        "load_effective_account",
        lambda account_id: {"account_fingerprint": "new-account-sha"},
    )
    monkeypatch.setattr(
        acceptance,
        "load_latest_plan",
        lambda account_id, account_fingerprint=None: None,
    )
    _, _, stale_payload = acceptance.run_v1_acceptance(
        output_root=tmp_path / "stale-account-acceptance"
    )
    assert stale_payload["checks"]["reference_plan_current_account_bound"] is False
    assert stale_payload["product_ready"] is False


def test_acceptance_rejects_a_plan_bound_to_an_old_daily_snapshot(
    tmp_path: Path, monkeypatch
) -> None:
    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    ranking = daily_dir / "ranking.csv"
    pd.DataFrame(
        [
            {
                "instrument_id": "000001.SZ",
                "alpha_score": 1,
                "selected": True,
                "target_weight": 0.05,
                "risk_context": "unknown",
            }
        ]
    ).to_csv(ranking, index=False)
    target = daily_dir / "target_portfolio.csv"
    target.write_text("instrument_id\n")
    report_path = daily_dir / "report.json"
    report_path.write_text("{}")
    html = daily_dir / "report.html"
    html.write_text("ok")
    snapshot = DailySnapshot(
        report_path,
        ranking,
        target,
        html,
        {
            "effective_as_of": "2026-09-09",
            "model": {"config_id": "daily_mvp_v1", "model_status": "baseline"},
            "content_fingerprint": "new-daily-sha",
        },
        True,
    )
    monkeypatch.setattr(
        acceptance, "inspect_data_status", lambda: {"effective_as_of": "2026-09-09"}
    )
    monkeypatch.setattr(acceptance, "load_validated_latest_snapshot", lambda: snapshot)
    monkeypatch.setattr(
        acceptance, "load_baseline_view", lambda: {"schema": "formal", "run_id": "run"}
    )
    monkeypatch.setattr(
        acceptance,
        "load_latest_factor_view",
        lambda: {"run_id": "factor", "signal_end": "2026-09-02"},
    )
    monkeypatch.setattr(acceptance, "list_accounts", lambda: ["demo_200k"])
    monkeypatch.setattr(
        acceptance,
        "load_effective_account",
        lambda account_id: {"account_fingerprint": "account-sha"},
    )
    monkeypatch.setattr(acceptance, "manual_tracking_fixture_smoke", lambda: True)
    monkeypatch.setattr(
        acceptance,
        "load_latest_plan",
        lambda account_id, account_fingerprint=None: (
            tmp_path / "plan.json",
            {
                "plan_id": "old",
                "account_fingerprint": "account-sha",
                "daily_content_fingerprint": "old-daily-sha",
                "csv_sha256": "csv-sha",
            },
        ),
    )
    monkeypatch.setattr(
        acceptance, "_git", lambda command: "" if command == "status --porcelain" else "a" * 40
    )
    _, _, payload = acceptance.run_v1_acceptance(output_root=tmp_path / "acceptance")
    assert payload["checks"]["reference_plan_current_daily_bound"] is False
    assert payload["product_ready"] is False
