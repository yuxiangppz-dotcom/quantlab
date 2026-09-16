"""Adversarial publication and input-change recovery checks; synthetic inputs only."""

import pandas as pd
import pytest
from test_ml_v2 import training_bundle

from quantlab.research.ml import runner, serving
from quantlab.research.ml.artifacts import verify_publication


def test_changed_input_invalidates_run_even_after_source_is_restored(tmp_path, monkeypatch):
    days, _, bundle, config = training_bundle(tmp_path)
    monkeypatch.setattr(runner, "code_identity", lambda root: {"synthetic": True})
    source = bundle / "features.parquet"
    original_bytes = source.read_bytes()
    fit = runner.walk_forward
    calls = []

    def changing(*args, **kwargs):
        result = fit(*args, **kwargs)
        calls.append(1)
        if len(calls) == 2:
            changed = pd.read_parquet(source)
            changed["f1"] = 0.0
            changed.to_parquet(source, index=False)
        return result

    monkeypatch.setattr(runner, "walk_forward", changing)
    output = tmp_path / "run"
    with pytest.raises(ValueError, match="fingerprint changed"):
        runner.run_training(bundle, config, output, days[60], days[85], root=tmp_path)
    assert (output / "invalidated.json").exists()
    assert len(list((output / "models").glob("*/completed.json"))) == 1
    source.write_bytes(original_bytes)
    monkeypatch.setattr(runner, "walk_forward", fit)
    with pytest.raises(ValueError, match="invalidated"):
        runner.run_training(bundle, config, output, days[60], days[85], root=tmp_path, resume=True)


def test_publication_crossing_deadline_is_not_forward(tmp_path, monkeypatch):
    days, panel, bundle, config = training_bundle(tmp_path)
    monkeypatch.setattr(runner, "code_identity", lambda root: {"synthetic": True})
    run = tmp_path / "run"
    runner.run_training(bundle, config, run, days[60], days[65], root=tmp_path)
    clock = [days[59].tz_localize("UTC")]
    monkeypatch.setattr(serving, "now", lambda: clock[0].to_pydatetime())
    registry = tmp_path / "registry"
    model = serving.register_model(run, str(days[60].to_period("M")), "ridge", registry)
    serving.activate_model(registry, model["model_id"], days[60].date())
    daily = tmp_path / "today.parquet"
    panel.loc[panel.trade_date.eq(days[60])].drop(columns="adj_close").to_parquet(
        daily, index=False
    )
    clock[0] = days[60].tz_localize("Asia/Shanghai") + pd.Timedelta(hours=15, minutes=59)
    write = serving.write_frame

    def delayed(*args, **kwargs):
        clock[0] += pd.Timedelta(minutes=2)
        return write(*args, **kwargs)

    monkeypatch.setattr(serving, "write_frame", delayed)
    out = tmp_path / "signals" / str(days[60].date())
    result = serving.predict_day(
        registry, daily, bundle / "calendar.json", days[60].date(), out, code={"synthetic": True}
    )
    assert not result["forward_eligible"]
    assert result["mode"] == "late_recomputation_not_forward"
    with pytest.raises(ValueError, match="genuinely"):
        serving.archived_signals(out.parent, [days[60].date()])
    (out / "published.json").unlink()
    with pytest.raises(ValueError, match="missing"):
        verify_publication(out, days[60].date())
