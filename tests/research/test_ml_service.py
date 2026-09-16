"""Real saved-model -> frozen decision -> next-session paper settlement integration."""

import json
from dataclasses import asdict, replace

import pandas as pd
import pytest
from test_ml_v2 import replay_fixture, training_bundle

from quantlab.research.ml import runner, service, serving
from quantlab.research.ml.artifacts import checkpoint_read
from quantlab.research.ml.config import load_config
from quantlab.research.ml.io import seal_bundle, sha256, write_json
from quantlab.research.ml.replay import replay_scores
from quantlab.research.quantity_scheduler import RawCloseMark, ResearchDay


@pytest.fixture
def system(tmp_path, monkeypatch):
    days, panel, bundle, config_path = training_bundle(tmp_path)
    (bundle / "manifest.json").unlink()
    write_json(
        bundle / "feature_contract.json",
        {"version": "synthetic-1", "feature_dependencies": {"f1": ["bars"], "f2": ["bars"]}},
    )
    lineage = panel[["trade_date", "instrument_id", "feature_available_at"]].rename(
        columns={"feature_available_at": "known_at"}
    )
    lineage["effective_at"] = lineage.known_at
    lineage["source_id"], lineage["revision_id"] = "bars", "synthetic"
    lineage.to_parquet(bundle / "pit_lineage.parquet", index=False)
    seal_bundle(bundle, provenance={"synthetic": True})
    monkeypatch.setattr(runner, "code_identity", lambda root: {"synthetic": True})
    run = tmp_path / "train"
    runner.run_training(bundle, config_path, run, days[60], days[65], root=tmp_path)
    clock = [days[59].tz_localize("UTC")]
    monkeypatch.setattr(serving, "now", lambda: clock[0].to_pydatetime())
    registry = tmp_path / "registry"
    model = serving.register_model(run, str(days[60].to_period("M")), "ridge", registry)
    serving.activate_model(registry, model["model_id"], days[60].date())
    config = load_config(config_path)
    marks = tuple(RawCloseMark(f"S{n}", days[60].date(), 1000) for n in range(6))
    root = tmp_path / "account"
    digest = sha256(bundle / "feature_contract.json")
    service.initialize(root, config, days, marks, days[60].date(), 10_000_000, digest)
    template = replay_fixture()[1][0].contexts[0]
    market, inputs = [], []
    for i in range(60, 67):
        folder = tmp_path / f"input-{i}"
        folder.mkdir()
        panel.loc[panel.trade_date.eq(days[i])].drop(columns="adj_close").to_parquet(
            folder / "features.parquet", index=False
        )
        write_json(folder / "calendar.json", days.strftime("%Y-%m-%d").tolist())
        contexts = tuple(
            replace(
                template,
                instrument_id=f"S{n}",
                execution_date=days[i].date(),
                next_session=days[i + 1].date(),
                evidence_date=days[i].date(),
                prior20_asof=days[i - 1].date(),
                rules=replace(
                    template.rules, effective_from=days[0].date(), effective_through=days[-1].date()
                ),
                fees=replace(
                    template.fees, effective_from=days[0].date(), effective_through=days[-1].date()
                ),
            )
            for n in range(6)
        )
        day = ResearchDay(
            days[i].date(),
            (),
            contexts,
            tuple(RawCloseMark(f"S{n}", days[i].date(), 1000) for n in range(6)),
            True,
        )
        (folder / "market.json").write_text(json.dumps(asdict(day), default=str))
        write_json(
            folder / "corporate_actions.json",
            {
                "coverage": {
                    "source_id": "synthetic",
                    "start": str(days[60].date()),
                    "end": str(days[-1].date()),
                },
                "events": [],
            },
        )
        write_json(
            folder / "manifest.json",
            {
                "asof": str(days[i].date()),
                "source_id": "synthetic",
                "available_at": (
                    days[i].tz_localize("Asia/Shanghai") + pd.Timedelta(hours=15, minutes=30)
                ).isoformat(),
                "feature_contract_sha256": digest,
                "files": {name: sha256(folder / name) for name in service.INPUT_FILES},
            },
        )
        market.append(day)
        inputs.append(folder)
    return root, registry, days, panel, inputs, clock, run, market, marks, config


def advance(system, n, *, late=False):
    root, registry, days, _, inputs, clock, *_ = system
    clock[0] = days[60 + n].tz_localize("Asia/Shanghai") + pd.Timedelta(
        hours=17 if late else 15, minutes=45
    )
    return service.run_day(root, inputs[n], registry, days[60 + n].date(), code={"synthetic": True})


def test_daily_ledger_matches_batch_replay_and_duplicate_is_noop(system):
    root, _, days, panel, _, _, run, market, marks, config = system
    for n in range(6):
        assert advance(system, n)["forward_decision"]
    before = (root / "sessions" / str(days[65].date()) / "completed.json").read_bytes()
    assert advance(system, 5)["forward_decision"]
    assert (root / "sessions" / str(days[65].date()) / "completed.json").read_bytes() == before
    expected = replay_scores(
        pd.read_parquet(run / "scores.parquet"),
        panel,
        tuple(d.date() for d in days),
        market[1:],
        marks,
        start=days[61].date(),
        end=days[65].date(),
        initial_cash_fen=10_000_000,
        config=config,
    )
    saved = checkpoint_read(root / "sessions" / str(days[65].date()) / "account.json")
    assert saved["state"]["book"] == expected.schedule.book
    assert saved["state"]["corporate_state"] == expected.corporate_state
    assert service.inspect_service(root)["sessions"] == 6
    assert saved["plan"]["execution_date"] == str(days[66].date())


def test_missing_day_is_rejected_and_late_catchup_does_not_create_orders(system):
    with pytest.raises(ValueError, match="noncontiguous"):
        advance(system, 1)
    assert not advance(system, 0, late=True)["forward_decision"]
    advance(system, 1)
    root, _, days, *_ = system
    saved = checkpoint_read(root / "sessions" / str(days[61].date()) / "account.json")
    assert not saved["state"]["book"].lots
    assert saved["settlement"]["decision_missing"]


def test_changed_input_cannot_rewrite_committed_day(system):
    advance(system, 0)
    root, registry, days, _, inputs, *_ = system
    manifest = inputs[0] / "manifest.json"
    raw = json.loads(manifest.read_text())
    raw["source_id"] = "revised"
    manifest.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="never rewrite"):
        service.run_day(root, inputs[0], registry, days[60].date(), code={"synthetic": True})


def test_expired_model_blocks_decision_but_preserves_account(system, monkeypatch):
    selected = serving.selected_model

    def expired(*args):
        folder, model = selected(*args)
        model["fit"]["fit_asof"] = "2000-01-01"
        return folder, model

    monkeypatch.setattr(serving, "selected_model", expired)
    result = advance(system, 0)
    assert not result["forward_decision"]
    assert "expired" in result["status"]
    assert service.inspect_service(system[0])["cash_fen"] == 10_000_000


def test_failed_publication_can_retry_without_retraining_or_double_settlement(system, monkeypatch):
    original = service.publish_ready

    def interrupted(*args, **kwargs):
        raise RuntimeError("interrupted before account publication")

    monkeypatch.setattr(service, "publish_ready", interrupted)
    with pytest.raises(RuntimeError, match="interrupted"):
        advance(system, 0)
    monkeypatch.setattr(service, "publish_ready", original)
    assert advance(system, 0)["forward_decision"]
    advance(system, 1)
    assert service.inspect_service(system[0])["sessions"] == 2


def test_missing_fee_stops_settlement_and_can_retry_after_evidence_repair(system):
    advance(system, 0)
    root, _, days, _, inputs, *_ = system
    market_path = inputs[1] / "market.json"
    manifest_path = inputs[1] / "manifest.json"
    market_original, manifest_original = market_path.read_bytes(), manifest_path.read_bytes()
    raw = json.loads(market_original)
    for context in raw["contexts"]:
        context["fees"] = None
    market_path.write_text(json.dumps(raw))
    manifest = json.loads(manifest_original)
    manifest["files"]["market.json"] = sha256(market_path)
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        advance(system, 1)
    assert not (root / "sessions" / str(days[61].date())).exists()
    assert service.inspect_service(root)["cash_fen"] == 10_000_000
    market_path.write_bytes(market_original)
    manifest_path.write_bytes(manifest_original)
    assert advance(system, 1)["forward_decision"]


def test_crash_after_rename_recovers_conservatively_without_backdating(system, monkeypatch):
    from quantlab.research.ml import artifacts

    original = artifacts.record_publication

    def interrupted(output, *args):
        if "sessions" in output.parts:
            raise RuntimeError("crash after rename")
        return original(output, *args)

    monkeypatch.setattr(artifacts, "record_publication", interrupted)
    with pytest.raises(RuntimeError, match="after rename"):
        advance(system, 0)
    monkeypatch.setattr(artifacts, "record_publication", original)
    assert not advance(system, 0, late=True)["forward_decision"]
    advance(system, 1)
    root, _, days, *_ = system
    saved = checkpoint_read(root / "sessions" / str(days[61].date()) / "account.json")
    assert not saved["state"]["book"].lots


def test_pause_preserves_settlement_and_prevents_new_decisions(system):
    advance(system, 0)
    service.set_paused(system[0], True)
    result = advance(system, 1)
    assert result["status"] == "paused_accounting_only"
    assert not result["forward_decision"]
    assert service.inspect_service(system[0])["lots"] > 0


def test_service_report_requires_complete_benchmark_and_retains_missing_decisions(system, tmp_path):
    from quantlab.research.ml.service_view import build_service_report

    advance(system, 0, late=True)
    advance(system, 1)
    root, _, days, *_ = system
    benchmark = tmp_path / "benchmark.parquet"
    pd.DataFrame({"session": [str(days[62].date())], "benchmark_return": [0.0]}).to_parquet(
        benchmark
    )
    with pytest.raises(ValueError, match="missing settled"):
        build_service_report(root, benchmark, tmp_path / "report")
    pd.DataFrame({"session": [str(days[61].date())], "benchmark_return": [0.0]}).to_parquet(
        benchmark
    )
    result = build_service_report(root, benchmark, tmp_path / "report")
    assert result["missing_decision_days"] == 1
    assert result["metrics"]["net_return"] == 0


def test_service_cli_status_and_run_day_exit_codes(system, monkeypatch, capsys):
    from quantlab.research.ml import cli

    root, registry, days, _, inputs, clock, *_ = system
    monkeypatch.setattr(cli, "code_identity", lambda root: {"synthetic": True})
    clock[0] = days[60].tz_localize("Asia/Shanghai") + pd.Timedelta(hours=17)
    assert (
        cli.main(
            [
                "run-day",
                "--service",
                str(root),
                "--registry",
                str(registry),
                "--inputs",
                str(inputs[0]),
                "--as-of",
                str(days[60].date()),
            ]
        )
        == 2
    )
    capsys.readouterr()
    assert cli.main(["status", "--path", str(root)]) == 0
    assert json.loads(capsys.readouterr().out)["schema"] == "quantlab_paper_service_v1"


def test_stale_account_and_failed_run_are_visible(system):
    advance(system, 0)
    with pytest.raises(ValueError, match="noncontiguous"):
        advance(system, 2, late=True)
    status = service.inspect_service(system[0])
    assert status["stale"]
    assert "noncontiguous" in status["latest_failure"]["reason"]


def test_service_rejects_injected_orders_in_market_evidence(system):
    folder = system[4][0]
    path = folder / "market.json"
    raw = json.loads(path.read_text())
    raw["orders"] = [{"order_id": "injected"}]
    path.write_text(json.dumps(raw))
    manifest = json.loads((folder / "manifest.json").read_text())
    manifest["files"]["market.json"] = sha256(path)
    (folder / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="inject orders"):
        advance(system, 0)


def test_service_code_change_prevents_account_commit(system):
    root, registry, days, _, inputs, clock, *_ = system
    clock[0] = days[60].tz_localize("Asia/Shanghai") + pd.Timedelta(hours=15, minutes=45)
    with pytest.raises(ValueError, match="code/runtime changed"):
        service.run_day(
            root,
            inputs[0],
            registry,
            days[60].date(),
            code={"synthetic": True},
            verify_code=lambda: {"different": True},
        )
    assert not (root / "sessions" / str(days[60].date())).exists()


def test_ml_workbench_renders_committed_account(system, tmp_path):
    from streamlit.testing.v1 import AppTest

    advance(system, 0)
    advance(system, 1)
    ui_root = tmp_path / "ui-root"
    experiments = ui_root / "data/experiments"
    experiments.mkdir(parents=True)
    (experiments / "paper").symlink_to(system[0], target_is_directory=True)

    def screen(path):
        from pathlib import Path

        from quantlab.ui.ml_workbench import render_ml_workbench

        render_ml_workbench(Path(path))

    app = AppTest.from_function(screen, args=(str(ui_root),)).run(timeout=30)
    assert not app.exception
    assert app.metric[0].label == "账户截止日"
    assert len(app.dataframe) == 4


def test_standalone_prediction_cli_uses_saved_model_and_code_check(
    system, monkeypatch, capsys, tmp_path
):
    from quantlab.research.ml import cli

    _, registry, days, _, inputs, clock, *_ = system
    monkeypatch.setattr(cli, "code_identity", lambda root: {"synthetic": True})
    clock[0] = days[60].tz_localize("Asia/Shanghai") + pd.Timedelta(hours=15, minutes=45)
    assert (
        cli.main(
            [
                "predict",
                "--registry",
                str(registry),
                "--features",
                str(inputs[0] / "features.parquet"),
                "--calendar",
                str(inputs[0] / "calendar.json"),
                "--as-of",
                str(days[60].date()),
                "--output",
                str(tmp_path / "standalone"),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["forward_eligible"]
