from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from quantlab.data.models import DataValidationError
from quantlab.research.forward_shadow import (
    _canonical_hash,
    _publish_prediction,
    _validate_existing,
    evaluate_matured_forward_shadows,
    generate_forward_shadow,
    latest_forward_shadow,
)
from quantlab.research.forward_shadow_analytics import (
    paired_shadow_diagnostics,
    summarize_forward_shadow,
)
from quantlab.research.shadow_timing import (
    PREDICTION_V1,
    prediction_timing,
    temporal_admission,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")


@pytest.mark.parametrize(
    ("created", "source", "status"),
    [
        ("2026-09-09T16:00:00+08:00", "2026-09-09T16:00:00+08:00",
         "eligible_same_signal_day"),
        ("2026-09-09T23:59:59.999999+08:00", "2026-09-09T16:00:00+08:00",
         "eligible_same_signal_day"),
        ("2026-09-09T08:00:00+00:00", "2026-09-09T08:00:00+00:00",
         "eligible_same_signal_day"),
        ("2026-09-09T15:59:59+08:00", "2026-09-09T15:59:59+08:00",
         "blocked_before_observation_window"),
        ("2026-09-09T18:00:00+08:00", "2026-09-09T15:59:59+08:00",
         "blocked_before_observation_window"),
        ("2026-09-10T00:00:00+08:00", "2026-09-09T18:00:00+08:00",
         "blocked_after_signal_day"),
        ("2026-09-09T16:00:00+00:00", "2026-09-09T08:00:00+00:00",
         "blocked_after_signal_day"),
        ("2026-10-20T18:00:00+08:00", "2026-10-20T17:00:00+08:00",
         "blocked_after_signal_day"),
    ],
)
def test_admission_window_uses_shanghai_date_and_exact_boundaries(created, source, status):
    result = temporal_admission("2026-09-09", created, source)
    assert result["status"] == status
    assert result["forward_eligible"] is (status == "eligible_same_signal_day")


@pytest.mark.parametrize("value", [None, 1, "invalid", "2026-09-09T18:00:00"])
def test_unknown_and_naive_timing_fails_closed(value):
    with pytest.raises(DataValidationError):
        temporal_admission("2026-09-09", value, "2026-09-09T17:00:00+08:00")
    with pytest.raises(DataValidationError):
        temporal_admission("2026-09-09", "2026-09-09T18:00:00+08:00", value)


def test_prediction_cannot_precede_its_daily_source():
    with pytest.raises(DataValidationError, match="predates"):
        temporal_admission(
            "2026-09-09", "2026-09-09T18:00:00+08:00", "2026-09-09T19:00:00+08:00",
        )


def _publish(root: Path, *, created: str = "2026-09-09T18:00:00+08:00"):
    import pandas as pd

    return _publish_prediction(
        root,
        {
            "schema": "quantlab_forward_shadow_prediction_v2",
            "trade_date": "2026-09-09",
            "model": {"model_id": "synthetic", "version": "v1"},
            "source_daily_generated_at": "2026-09-09T17:00:00+08:00",
            "daily_report_sha256": "a" * 64,
            "label": {"horizon_sessions": 20},
        },
        pd.DataFrame({"instrument_id": ["000001.SZ"], "alpha_score": [1.0]}),
        pd.DataFrame({"instrument_id": ["000001.SZ"], "target_weight": [1.0]}),
        datetime.fromisoformat(created),
    )


@pytest.mark.parametrize("field", ["created_at", "source_daily_generated_at"])
def test_bound_timestamps_cannot_be_changed_without_invalidating_hash(tmp_path, field):
    result = _publish(tmp_path)
    marker = result.prediction_dir / "prediction.json"
    payload = json.loads(marker.read_text())
    payload[field] = "2026-09-09T17:30:00+08:00"
    marker.write_text(json.dumps(payload))
    with pytest.raises(DataValidationError, match="manifest content mismatch"):
        latest_forward_shadow(tmp_path)


def test_rehashed_forged_admission_still_rejected(tmp_path):
    result = _publish(tmp_path, created="2026-10-20T18:00:00+08:00")
    marker = result.prediction_dir / "prediction.json"
    payload = json.loads(marker.read_text())
    payload["temporal_admission"]["forward_eligible"] = True
    core = {k: v for k, v in payload.items()
            if k not in {"prediction_fingerprint", "files_sha256", "claims"}}
    digest = _canonical_hash({"core": core, "files_sha256": payload["files_sha256"]})
    payload["prediction_fingerprint"] = digest
    marker.write_text(json.dumps(payload))
    with pytest.raises(DataValidationError, match="temporal admission mismatch"):
        _validate_existing(marker.parent, digest)


def test_retry_preserves_first_time_and_rejects_clock_reversal(tmp_path):
    first = _publish(tmp_path)
    marker = first.prediction_dir / "prediction.json"
    original = marker.read_bytes()
    retry = _publish(tmp_path, created="2026-10-20T18:00:00+08:00")
    assert retry.reused
    assert retry.prediction_fingerprint == first.prediction_fingerprint
    assert marker.read_bytes() == original
    with pytest.raises(DataValidationError, match="retry predates"):
        _publish(tmp_path, created="2026-09-09T17:30:00+08:00")
    assert marker.read_bytes() == original


def test_legacy_timestamp_never_becomes_forward_eligible():
    manifest = {"schema": PREDICTION_V1, "created_at": "2000-01-01T00:00:00+08:00"}
    assert prediction_timing(manifest)["status"] == "blocked_legacy_unverified_time"
    with pytest.raises(DataValidationError, match="unsupported"):
        prediction_timing({"schema": "unknown"})


def test_late_prediction_does_not_reach_labels_or_forward_statistics(tmp_path):
    result = _publish(tmp_path / "shadow", created="2026-10-20T18:00:00+08:00")

    class CalendarOnlyStorage:
        def load_trading_calendar(self):
            return []

        def daily_bars_exists(self, day):
            pytest.fail("ineligible prediction must not consume future labels")

    assert evaluate_matured_forward_shadows(
        storage=CalendarOnlyStorage(), shadow_root=tmp_path / "shadow",
    ) == []
    summary = summarize_forward_shadow(tmp_path / "shadow")[0]
    assert summary.prediction_count == summary.excluded_prediction_count == 1
    assert summary.complete_evaluation_count == summary.pending_prediction_count == 0
    assert summary.mean_weighted_target_return is None
    assert result.prediction_dir.exists()


def test_stale_daily_is_archived_but_not_counted_as_mature_forward(tmp_path):
    from test_forward_shadow import _seed

    storage, products, config, _ = _seed(tmp_path)
    root = tmp_path / "shadow"
    generate_forward_shadow(
        product_root=products, shadow_root=root, config_path=config,
        now=datetime(2026, 3, 1, 19, tzinfo=SHANGHAI),
    )
    assert evaluate_matured_forward_shadows(storage=storage, shadow_root=root) == []
    assert all(s.excluded_prediction_count == 1 for s in summarize_forward_shadow(root))
    paired = paired_shadow_diagnostics(root, ("baseline", "v1"), ("combo", "v1"))
    assert paired["paired_complete_count"] == 0
    assert paired["mean_paired_difference"] is None


def test_legacy_duplicates_and_complete_evaluations_remain_excluded(tmp_path):
    from test_forward_shadow_analytics import _write_evaluation

    root = tmp_path / "shadow"
    for sequence in range(2):
        result = _publish(tmp_path / f"seed-{sequence}")
        original = result.prediction_dir / "prediction.json"
        payload = json.loads(original.read_text())
        payload["schema"] = PREDICTION_V1
        payload["code_head"] = str(sequence) * 40
        for field in ("source_daily_generated_at", "daily_report_sha256", "temporal_admission"):
            del payload[field]
        core = {k: v for k, v in payload.items()
                if k not in {"prediction_fingerprint", "files_sha256", "claims", "created_at"}}
        digest = _canonical_hash({"core": core, "files_sha256": payload["files_sha256"]})
        payload["prediction_fingerprint"] = digest
        out = root / "synthetic" / "v1" / "2026-09-09" / digest
        out.mkdir(parents=True)
        for filename in ("scores.csv", "target_portfolio.csv"):
            (out / filename).write_bytes((result.prediction_dir / filename).read_bytes())
        (out / "prediction.json").write_text(json.dumps(payload))
        _write_evaluation(
            root / "evaluations", model_id="synthetic", version="v1",
            signal_date="2026-09-09", prediction_fingerprint=digest,
            status="complete", value=0.25,
        )
    summary = summarize_forward_shadow(root)[0]
    assert summary.prediction_count == summary.excluded_prediction_count == 2
    assert summary.complete_evaluation_count == 0
    assert summary.mean_weighted_target_return is None
    assert paired_shadow_diagnostics(
        root, ("synthetic", "v1"), ("synthetic", "v1"),
    )["paired_complete_count"] == 0


def test_frozen_identity_rejects_changed_inputs_and_preserves_original(tmp_path):
    from test_forward_shadow import _seed

    _, products, config, signal = _seed(tmp_path)
    root = tmp_path / "shadow"
    now = datetime.combine(signal, datetime.min.time(), SHANGHAI).replace(hour=19)
    result = generate_forward_shadow(
        product_root=products, shadow_root=root, config_path=config, now=now,
    )[0]
    marker = result.prediction_dir / "prediction.json"
    original = marker.read_bytes()
    changed = json.loads(config.read_text())
    changed["max_weight_per_name"] = 0.5
    config.write_text(json.dumps(changed))
    with pytest.raises(DataValidationError, match="already frozen with other inputs"):
        generate_forward_shadow(
            product_root=products, shadow_root=root, config_path=config, now=now,
        )
    assert marker.read_bytes() == original


def test_numeric_admission_flag_is_not_boolean_authority(tmp_path):
    result = _publish(tmp_path)
    payload = json.loads((result.prediction_dir / "prediction.json").read_text())
    payload["temporal_admission"]["forward_eligible"] = 1
    with pytest.raises(DataValidationError, match="temporal admission mismatch"):
        prediction_timing(payload)


def test_paired_diagnostic_rejects_cross_model_evaluation(tmp_path):
    from test_forward_shadow_analytics import _write_evaluation

    result = _publish(tmp_path)
    _write_evaluation(
        tmp_path / "evaluations", model_id="wrong-model", version="v1",
        signal_date="2026-09-09", prediction_fingerprint=result.prediction_fingerprint,
        status="complete", value=0.25,
    )
    with pytest.raises(ValueError, match="identity does not match"):
        paired_shadow_diagnostics(tmp_path, ("synthetic", "v1"), ("synthetic", "v1"))


@pytest.mark.parametrize("horizon", [True, 0, -1, 1.5, "20", None])
def test_invalid_label_horizon_cannot_enter_temporal_evidence(tmp_path, horizon):
    result = _publish(tmp_path)
    payload = json.loads((result.prediction_dir / "prediction.json").read_text())
    payload["label"]["horizon_sessions"] = horizon
    with pytest.raises(DataValidationError, match="positive integer"):
        prediction_timing(payload)


def test_duplicate_artifact_at_another_path_cannot_inflate_forward_count(tmp_path):
    import shutil

    result = _publish(tmp_path)
    shutil.copytree(result.prediction_dir, result.prediction_dir.parent / ("0" * 64))
    with pytest.raises(DataValidationError, match="path identity mismatch"):
        summarize_forward_shadow(tmp_path)
