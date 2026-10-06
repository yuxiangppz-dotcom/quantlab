"""Maintenance claims and observation failures never become paid retry authority."""
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from quantlab.scout import free_cloud as free
from quantlab.scout.cloud_artifacts import initialize
from quantlab.scout.daily_runtime import read
from quantlab.scout.models import SHANGHAI, fingerprint


class Cloud:
    def __init__(self):
        self.events = []
        self.claimed = False

    def request(self, path, **kwargs):
        self.events.append((path, kwargs))
        if path == "/api/maintenance/claim":
            if self.claimed:
                return {"status": "already_completed"}
            self.claimed = True
            return kwargs["value"] | {"status": "claimed", "owner": "owner"}
        if path == "/api/snapshot":
            return {"snapshot": None}
        return {"status": "saved"}


class Archive:
    def __init__(self, root, client, job):
        import threading
        self.client, self.error, self.stop = client, None, threading.Event()

    def checkpoint(self):
        self.client.events.append(("checkpoint", {}))

    def background(self):
        self.stop.wait()


def test_maintenance_claim_archive_then_bounded_prepare_no_notify(tmp_path, monkeypatch):
    cloud = Cloud()
    monkeypatch.setattr(free, "Archive", Archive)
    calls = []

    def factory(root, now):
        assert [p for p, _ in cloud.events][-1] == "checkpoint"
        calls.append("provider factory")
        return object()

    def prepare(root, fetcher, now, *, partition_limit):
        assert partition_limit == 40
        return {"status": "pending", "remaining_partitions": 30}

    monkeypatch.setattr(free, "prepare_history", prepare)
    settings = {"canonical_dir": str(tmp_path / "scout/market")}
    kwargs = dict(app_commit="a" * 40, kind="prepare", batch=7,
                  now=datetime(2026, 10, 6, 12, tzinfo=SHANGHAI), fetcher_factory=factory)
    result = free.maintenance(settings, cloud, **kwargs)
    assert result["model_calls"] == 0 and result["maintenance_status"] == "pending"
    assert cloud.events[-1][0] == "/api/maintenance/finish"
    assert not any(path in {"/api/claim", "/api/finish", "/api/notify"}
                   for path, _ in cloud.events)
    assert free.maintenance(settings, cloud, **kwargs)["status"] == "already_completed"
    assert calls == ["provider factory"]
    assert list((tmp_path / "scout/observations/maintenance_jobs").glob("*.json"))


def test_bad_window_and_batch_contact_nothing(tmp_path):
    cloud = Cloud()
    result = free.maintenance({}, cloud, app_commit="a" * 40, kind="observation",
                              now=datetime(2026, 10, 6, 17, tzinfo=SHANGHAI))
    assert result["status"] == "outside_observation_window" and not cloud.events
    with pytest.raises(ValueError, match="batch"):
        free.maintenance({}, cloud, app_commit="a" * 40, kind="prepare", batch=8)
    assert not cloud.events


def test_catchup_missing_data_still_scans_and_saves_unknown(tmp_path, monkeypatch):
    root = initialize(tmp_path / "scout")
    calls = []

    def unavailable(*args):
        raise ValueError("provider missing")

    def scan(*args, **kwargs):
        calls.append(kwargs)
        return {"status": "complete", "model_calls": 0}

    result = free.observation_catchup(root, datetime(2026, 10, 6, 18, tzinfo=SHANGHAI),
                                     fetcher_factory=unavailable, scan=scan)
    assert result["status"] == "data_unknown"
    assert calls[0]["eligible_target_sessions"] is None
    assert result["model_calls"] == 0 and result["provider_calls"] == 0
    assert read(next((root / "observations/jobs").glob("*.json"))) == result


def test_coverage_denominator_starts_actual_activation_not_old_calendar(tmp_path, monkeypatch):
    root = initialize(tmp_path / "scout")
    days = [date(2026, 9, 30), date(2026, 10, 6), date(2026, 10, 8)]
    storage = SimpleNamespace(load_trading_calendar=lambda: [
        SimpleNamespace(trade_date=day, exchange="SSE", is_open=True) for day in days])
    monkeypatch.setattr(free, "refresh_calendar", lambda *args: storage)
    monkeypatch.setattr(free, "refresh_market", lambda *args, **kwargs: {"status": "ready"})
    seen = []
    def scan(*args, **kwargs):
        seen.append(kwargs)
        return {"status": "complete"}

    def factory(*args):
        return SimpleNamespace(calls=0)
    free.observation_catchup(root, datetime(2026, 10, 6, 8, tzinfo=SHANGHAI),
                            fetcher_factory=factory, scan=scan)
    assert seen[-1]["eligible_target_sessions"] == []
    free.observation_catchup(root, datetime(2026, 10, 8, 18, tzinfo=SHANGHAI),
                            fetcher_factory=factory, scan=scan)
    assert seen[-1]["eligible_target_sessions"] == ["2026-10-06", "2026-10-08"]

def test_actual_publication_receipt_is_bound_to_original_and_archived_before_finish(
    tmp_path, monkeypatch
):
    import json
    root = initialize(tmp_path / "scout")
    run = root / "runs/nested/synthetic-run"
    run.mkdir(parents=True)
    original = b'{"run_id":"synthetic-run"}'
    (run / "report.json").write_bytes(original)
    report_hash = fingerprint({"run_id": "synthetic-run"})
    publication = {"run_id": "synthetic-run", "source_report_sha256": report_hash,
                   "status": "published", "published_at": "2026-10-08T08:30:00+08:00"}

    class Delivery(Cloud):
        def request(self, path, **kwargs):
            if path == "/api/claim":
                self.events.append((path, kwargs))
                return {"status": "claimed", "day": "2026-10-08", "owner": "owner"}
            if path == "/api/publish":
                self.events.append((path, kwargs))
                return {"status": "published", "publication": publication}
            if path == "/api/finish":
                assert self.events[-1][0] == "checkpoint"
                assert json.loads((run / "cloud-publication.json").read_text()) == publication
            return super().request(path, **kwargs)

    def publish(settings, now, **kwargs):
        view = root / "reports/synthetic-run"
        view.mkdir(parents=True)
        (view / "report.html").write_text("synthetic")
        (view / "report.md").write_text("synthetic")
        return {"status": "published", "report": {
            "run_id": "synthetic-run", "source_report_sha256": report_hash}}

    monkeypatch.setattr(free, "Archive", Archive)
    cloud = Delivery()
    result = free.execute({"canonical_dir": str(root / "market")}, cloud,
                          app_commit="b" * 40, now=datetime(2026, 10, 8, 8, tzinfo=SHANGHAI),
                          run=publish, catchup=lambda *args: None)
    assert result["status"] == "published"
    assert (run / "report.json").read_bytes() == original
