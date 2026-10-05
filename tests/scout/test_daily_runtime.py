import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytest

from quantlab.scout import daily_runtime as runtime
from quantlab.scout.models import SHANGHAI, fingerprint


def settings(tmp_path):
    release = tmp_path / "release"
    release.mkdir()
    (release / "fixed.txt").write_text("fixed")
    config = {
        "release_root": str(release),
        "canonical_dir": str(tmp_path / "canonical"),
        "output_root": str(tmp_path / "runs"),
        "state_root": str(tmp_path / "state"),
    }
    runtime.atomic(
        release / "daily-manifest.json",
        {
            "files": {"fixed.txt": hashlib.sha256(b"fixed").hexdigest()},
            "settings_sha256": fingerprint(config),
            "commit": "immutable-version",
            "config_sha256": "fixed-config",
        },
    )
    return config


def ready(monkeypatch):
    details = {
        "timing": {"asof_session": "2026-09-30", "target_session": "2026-10-08"},
        "version": "immutable-version",
        "holiday": True,
    }
    monkeypatch.setattr(runtime, "precheck", lambda _: ("same-day-key", details))


def test_concurrent_clicks_and_failed_jobs_never_create_second_paid_task(tmp_path, monkeypatch):
    config = settings(tmp_path)
    ready(monkeypatch)
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: runtime.claim(config), range(8)))
    assert sum(created for _, _, created in results) == 1
    paths = {path for path, _, _ in results}
    assert len(paths) == 1
    path = paths.pop()
    runtime.update(path, "unknown delivery", status="failed", delivery_status="unknown")
    assert runtime.claim(config)[2] is False


def test_fixed_release_and_settings_change_are_rejected(tmp_path):
    config = settings(tmp_path)
    assert runtime.verify_release(config)["commit"] == "immutable-version"
    with pytest.raises(ValueError, match="settings_changed"):
        runtime.verify_release(config | {"output_root": "different"})
    (tmp_path / "release/fixed.txt").write_text("changed")
    with pytest.raises(ValueError, match="release_changed"):
        runtime.verify_release(config)


def test_holiday_target_and_stale_data_prevent_call(tmp_path, monkeypatch):
    from datetime import date
    from types import SimpleNamespace

    config = settings(tmp_path)
    monkeypatch.setattr(
        runtime,
        "inspect_market_data",
        lambda *_: {"live_partition_files_present": True, "expected_session": "2026-09-30"},
    )
    monkeypatch.setattr(
        runtime.ParquetStorage,
        "load_trading_calendar",
        lambda _: [
            SimpleNamespace(trade_date=date(2026, 9, 30), exchange="SSE", is_open=True),
            SimpleNamespace(trade_date=date(2026, 10, 8), exchange="SSE", is_open=True),
        ],
    )
    _, result = runtime.precheck(config, datetime(2026, 10, 5, 12, tzinfo=SHANGHAI))
    assert result["holiday"] and result["timing"]["target_session"] == "2026-10-08"
    monkeypatch.setattr(
        runtime, "inspect_market_data", lambda *_: {"live_partition_files_present": False}
    )
    with pytest.raises(ValueError, match="未准备好"):
        runtime.claim(config)
    assert not list((tmp_path / "state").glob("jobs/*"))


def test_worker_failure_preserves_status_without_followup(tmp_path, monkeypatch):
    config = settings(tmp_path)
    ready(monkeypatch)
    path, _, _ = runtime.claim(config)
    monkeypatch.setattr(
        runtime, "run_scout", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("fixture"))
    )
    # Test fixture deliberately has no config; failure still gives a durable failed job.
    runtime.worker(config, path)
    assert runtime.read(path)["status"] == "failed"
    assert runtime.claim(config)[2] is False
