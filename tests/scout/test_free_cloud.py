"""Durable archive boundaries and crash/duplicate suppression on ephemeral runners."""

import gzip
import io
from datetime import datetime

import pytest

from quantlab.scout import free_cloud as free
from quantlab.scout.cloud_artifacts import initialize
from quantlab.scout.models import SHANGHAI


class MemoryCloud:
    def __init__(self):
        self.chunks = {}
        self.snapshot = None
        self.job = None
        self.paths = []

    def request(self, path, *, method="GET", value=None, binary=None, job=None):
        self.paths.append(path)
        if path == "/api/claim":
            if self.job:
                return {"status": "already_claimed"}
            self.job = {"status": "claimed", "day": value["day"], "owner": "owner"}
            return self.job.copy()
        if path == "/api/snapshot":
            return {"snapshot": self.snapshot}
        if path.startswith("/api/blobs/"):
            key = path.rsplit("/", 1)[1]
            if method == "PUT":
                assert free.sha(binary) == key
                self.chunks[key] = binary
                return {"status": "saved"}
            return self.chunks[key]
        if path == "/api/checkpoint":
            self.snapshot = value["snapshot"]
            return {"status": "checkpoint_saved"}
        return {"status": "saved"}


def test_archive_restores_bytes_and_deduplicates_without_cache(tmp_path):
    cloud = MemoryCloud()
    root = initialize(tmp_path / "one/scout")
    (root / "runs/run").mkdir(parents=True)
    original = b"real saved bytes" * 10000
    (root / "runs/run/response.json").write_bytes(original)
    archive = free.Archive(root, cloud, {"day": "2026-10-08", "owner": "owner"})
    archive.checkpoint()
    puts = cloud.paths.count("/api/checkpoint")
    archive.checkpoint()
    assert cloud.paths.count("/api/checkpoint") == puts
    restored = initialize(tmp_path / "two/scout")
    free.restore(restored, cloud.snapshot, cloud)
    assert (restored / "runs/run/response.json").read_bytes() == original


@pytest.mark.parametrize(
    "path",
    ["../secret", "/absolute", "runs/../../secret", "runs\\x", "runs//x", ".env", "runs/x.log"],
)
def test_archive_rejects_paths(path):
    with pytest.raises(ValueError):
        free.safe_path(path)


def test_archive_refuses_credentials_and_symlinks(tmp_path, monkeypatch):
    root = initialize(tmp_path / "scout")
    (root / "runs").mkdir()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-private-key-12345678")
    (root / "runs/response.json").write_text("fake-private-key-12345678")
    archive = free.Archive(root, MemoryCloud(), {})
    with pytest.raises(ValueError, match="secret"):
        archive.checkpoint()
    (root / "runs/response.json").write_text("safe")
    (root / "runs/link").symlink_to(tmp_path)
    with pytest.raises(ValueError, match="symlink"):
        archive.checkpoint()


def test_restore_rejects_tampered_chunks_and_gzip_bomb(tmp_path, monkeypatch):
    root = initialize(tmp_path / "scout")
    cloud = MemoryCloud()
    data = gzip.compress(b"x" * 100)
    key = free.sha(data)
    cloud.chunks[key] = data
    files = [{"path": "runs/x.json", "sha256": free.sha(b"x" * 100), "size": 1, "chunks": [key]}]
    snapshot = {"files": files, "sha256": free.sha(free.encoded(files))}
    monkeypatch.setattr(free, "MAX_FILE", 10)
    with pytest.raises(ValueError, match="file_integrity"):
        free.restore(root, snapshot, cloud)
    cloud.chunks[key] = b"corrupt"
    with pytest.raises(ValueError, match="chunk_integrity"):
        free.restore(root, snapshot, cloud)


def test_remote_claim_precedes_provider_work_and_duplicate_never_runs(tmp_path):
    root = tmp_path / "scout"
    settings = {"canonical_dir": str(root / "market")}
    cloud = MemoryCloud()
    calls = []

    def run(*args, **kwargs):
        assert cloud.job and cloud.snapshot
        assert "/api/checkpoint" in cloud.paths
        calls.append("once")
        return {"status": "non_trading_day"}

    now = datetime(2026, 10, 8, 8, 3, tzinfo=SHANGHAI)
    assert (
        free.execute(settings, cloud, app_commit="a" * 40, now=now, run=run)["status"]
        == "non_trading_day"
    )
    assert (
        free.execute(settings, cloud, app_commit="a" * 40, now=now, run=run)["status"]
        == "already_claimed"
    )
    assert calls == ["once"] and "/api/notify" not in cloud.paths


def test_archive_failure_retains_claim_and_unknown_state(tmp_path):
    root = tmp_path / "scout"
    cloud = MemoryCloud()

    def broken(*args, **kwargs):
        (root / "runs").mkdir()
        (root / "runs/response.json").write_text("saved before failure")
        raise ValueError("model delivery unknown")

    with pytest.raises(ValueError, match="model delivery unknown"):
        free.execute(
            {"canonical_dir": str(root / "market")},
            cloud,
            app_commit="a" * 40,
            now=datetime(2026, 10, 8, 8, tzinfo=SHANGHAI),
            run=broken,
        )
    assert "/api/finish" not in cloud.paths
    assert any(r["path"] == "runs/response.json" for r in cloud.snapshot["files"])


def test_late_job_never_contacts_archive_or_provider(tmp_path):
    cloud = MemoryCloud()
    assert (
        free.execute({}, cloud, app_commit="a" * 40, now=datetime(2026, 10, 8, 9, tzinfo=SHANGHAI))[
            "status"
        ]
        == "outside_morning_window"
    )
    assert not cloud.paths


def test_cloud_client_identifies_its_own_automation_transport():
    class Opener:
        def open(self, request, *, timeout):
            assert request.get_header("User-agent") == "QuantLab-Scout-Cloud/1.0"
            assert request.get_header("Authorization") == "Bearer " + "x" * 48
            assert request.full_url == "https://scout.example/api/snapshot"
            assert timeout == 30
            return io.BytesIO(b'{"snapshot":null}')

    client = free.Client("https://scout.example", "x" * 48, opener=Opener())
    assert client.request("/api/snapshot") == {"snapshot": None}


def test_complete_report_text_reaches_durable_cloud_before_notification(tmp_path):
    class DeliveryCloud(MemoryCloud):
        def request(self, path, **kwargs):
            if path == "/api/publish":
                self.report = kwargs["value"]
            if path == "/api/notify":
                assert self.report["markdown"] == "synthetic candidate and reason"
                assert self.report["markdown_sha256"] == free.sha(self.report["markdown"].encode())
                assert self.paths.index("/api/publish") < self.paths.index("/api/finish")
            return super().request(path, **kwargs)

    root = initialize(tmp_path / "scout")
    cloud = DeliveryCloud()

    def publish_saved_fixture(settings, now, **kwargs):
        folder = root / "reports" / "synthetic-run"
        folder.mkdir(parents=True)
        (folder / "report.html").write_text("synthetic html")
        (folder / "report.md").write_text("synthetic candidate and reason")
        return {"status": "published", "report": {"run_id": "synthetic-run"}}

    result = free.execute(
        {"canonical_dir": str(root / "market")}, cloud, app_commit="a" * 40,
        now=datetime(2026, 10, 8, 8, 3, tzinfo=SHANGHAI), run=publish_saved_fixture,
    )
    assert result["status"] == "published"
    assert cloud.paths[-1] == "/api/notify"
