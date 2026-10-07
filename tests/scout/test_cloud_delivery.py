"""Cloud authority, durable idempotency, private phone viewing and source units."""

import hashlib
import json
import threading
from datetime import date, datetime
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

import pandas as pd
import pytest

from quantlab.scout import cloud_runner as runner
from quantlab.scout.cloud_artifacts import checked_report, guarded, initialize, publish, read_view
from quantlab.scout.cloud_data import Fetcher, convert, refresh_calendar, refresh_market
from quantlab.scout.cloud_push import notify, notify_failure
from quantlab.scout.cloud_web import make_server, password_hash, signed_cookie, valid_cookie
from quantlab.scout.daily_runtime import atomic, read
from quantlab.scout.models import SHANGHAI, fingerprint


@pytest.fixture
def volume(tmp_path):
    return initialize(tmp_path / "scout")


def saved_run(root):
    run_id = "20261008T081500-fixture"
    folder = root / "runs" / run_id
    folder.mkdir(parents=True)
    report = {
        "run_id": run_id,
        "status": "live_research_unvalidated",
        "finished_at": "2026-10-08T08:15:00+08:00",
        "market": {"session": "2026-09-30"},
        "timing": {"target_session": "2026-10-08", "primary_eligible": True},
        "opportunity": {"validation": {"status": "complete"}},
        "candidates": [{"instrument_id": "000001.SZ", "name": "测试股"}],
        "selection": {
            "selected": [
                {
                    "instrument_id": "000001.SZ",
                    "status": "focus",
                    "thesis": "fixture理由",
                    "risk": "fixture风险",
                    "invalidation": "fixture失效",
                }
            ]
        },
    }
    atomic(folder / "report.json", report)
    atomic(folder / "manifest.json", {"report_sha256": fingerprint(report)})
    return folder


def test_volume_does_not_adopt_existing_data_or_follow_external_symlink(tmp_path, volume):
    legacy = tmp_path / "legacy/scout"
    legacy.mkdir(parents=True)
    (legacy / "old.txt").write_text("preserve")
    with pytest.raises(ValueError, match="nonempty"):
        initialize(legacy)
    (volume / "market").symlink_to(legacy, target_is_directory=True)
    with pytest.raises(ValueError, match="outside"):
        guarded(volume)
    assert (legacy / "old.txt").read_text() == "preserve"


@pytest.mark.parametrize("change", ["status", "eligibility", "validation", "hash"])
def test_failed_or_derived_report_never_published(volume, change):
    folder = saved_run(volume)
    report = read(folder / "report.json")
    if change == "status":
        report["status"] = "incomplete"
    elif change == "eligibility":
        report["timing"]["primary_eligible"] = False
    elif change == "validation":
        report["opportunity"]["validation"]["status"] = "incomplete"
    else:
        report["selection"]["selected"][0]["thesis"] = "tampered"
    atomic(folder / "report.json", report)
    if change != "hash":
        atomic(folder / "manifest.json", {"report_sha256": fingerprint(report)})
    with pytest.raises(ValueError):
        checked_report(folder)
    assert not (volume / "latest.json").exists()


def test_publish_preserves_source_and_reuses_same_immutable_view(volume):
    folder = saved_run(volume)
    before = {p.name: p.read_bytes() for p in folder.iterdir()}
    metadata = publish(volume, folder)
    assert publish(volume, folder) == metadata
    assert before == {p.name: p.read_bytes() for p in folder.iterdir()}
    assert read_view(volume, metadata["run_id"]).count(b"<article") == 1
    (volume / "reports" / metadata["run_id"] / "report.html").write_text("changed")
    with pytest.raises(ValueError, match="integrity"):
        read_view(volume, metadata["run_id"])


@pytest.mark.parametrize("outcome", ["accepted", "unknown", "rejected"])
def test_push_intent_precedes_send_and_never_retries_unknown(volume, monkeypatch, outcome):
    monkeypatch.setenv("SERVERCHAN_SENDKEY", "SCTfixture0123456789")
    metadata = publish(volume, saved_run(volume))
    calls = []

    def transport(key, payload):
        assert len(list((volume / "outbox").glob("*.json"))) == 1
        calls.append(payload)
        assert "SCT" not in payload["desp"] and "000001" not in payload["desp"]
        if outcome == "unknown":
            raise TimeoutError("SCTfixture0123456789 must never appear in receipt")
        return {"code": 0 if outcome == "accepted" else 1}

    first = notify(volume, metadata, "https://scout.example.com", transport=transport)
    second = notify(volume, metadata, "https://scout.example.com", transport=transport)
    assert first == second and len(calls) == 1
    assert (
        first["status"]
        == {"accepted": "provider_accepted", "unknown": "delivery_unknown", "rejected": "rejected"}[
            outcome
        ]
    )
    assert "SCTfixture" not in json.dumps(first)


def test_missing_push_config_can_be_added_without_repeating_prediction(volume, monkeypatch):
    metadata = publish(volume, saved_run(volume))
    monkeypatch.delenv("SERVERCHAN_SENDKEY", raising=False)
    assert notify(volume, metadata, "https://scout.example.com")["status"] == "not_configured"
    monkeypatch.setenv("SERVERCHAN_SENDKEY", "sctp123tfixture000000")
    assert (
        notify(volume, metadata, "https://scout.example.com")["status"] == "invalid_turbo_sendkey"
    )
    assert not (volume / "outbox").exists()


def test_failure_notification_is_not_a_new_report_and_is_sent_once(volume, monkeypatch):
    from quantlab.scout import cloud_push

    monkeypatch.setenv("SERVERCHAN_SENDKEY", "SCTfixture0123456789")
    calls = []
    monkeypatch.setattr(
        cloud_push, "send_http", lambda key, payload: calls.append(payload) or {"code": 0}
    )
    notify_failure(volume, "2026-10-08", "https://scout.example.com")
    notify_failure(volume, "2026-10-08", "https://scout.example.com")
    assert len(calls) == 1 and "未发布新候选" in calls[0]["desp"]
    assert "已生成" not in calls[0]["title"] and not (volume / "latest.json").exists()


def test_cookie_cannot_be_forged_expired_or_used_with_another_secret():
    secret = "x" * 32
    value = signed_cookie(secret, 100)
    assert valid_cookie(value, secret, 101)
    assert not valid_cookie(value, secret, 100 + 7 * 86400)
    assert not valid_cookie(value + "a", secret, 101)
    assert not valid_cookie(value, "y" * 32, 101)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def test_real_http_login_private_report_and_absence_of_paid_endpoint(volume):
    metadata = publish(volume, saved_run(volume))
    password = "fixture-password-1234"
    server = make_server(
        volume, "https://scout.example.com", password_hash(password), "x" * 32, port=0
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    opener = build_opener(NoRedirect())
    try:
        path = "/reports/" + metadata["run_id"]
        with opener.open(base + "/login") as login:
            assert login.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
        with pytest.raises(HTTPError) as unavailable:
            opener.open(base + path)
        assert unavailable.value.code == 303
        for origin, expected in (
            ("https://evil.example", 403),
            ("null", 403),
            ("https://scout.example.com", 303),
        ):
            request = Request(
                base + "/login",
                data=urlencode({"password": password, "next": path}).encode(),
                headers={"Origin": origin},
            )
            with pytest.raises(HTTPError) as result:
                opener.open(request)
            assert result.value.code == expected
        cookie = result.value.headers["Set-Cookie"]
        assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=Lax" in cookie
        assert result.value.headers["Location"] == path
        with opener.open(Request(base + path, headers={"Cookie": cookie})) as response:
            body = response.read().decode()
        assert "fixture理由" in body and "信息源覆盖" not in body
        with pytest.raises(HTTPError) as forbidden:
            opener.open(
                Request(
                    base + "/run",
                    data=b"{}",
                    headers={"Cookie": cookie, "Origin": "https://scout.example.com"},
                )
            )
        assert forbidden.value.code == 404
        with pytest.raises(HTTPError) as traversal:
            opener.open(Request(base + "/reports/../source_updates", headers={"Cookie": cookie}))
        assert traversal.value.code == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_provider_units_date_and_duplicates():
    day = date(2026, 9, 30)
    row = {
        "ts_code": "000001.SZ",
        "trade_date": "20260930",
        "open": 10,
        "high": 11,
        "low": 9,
        "close": 10,
        "pre_close": 9.8,
        "vol": 123,
        "amount": 456,
        "turnover_rate": 5.2,
        "total_mv": 100,
        "circ_mv": 90,
    }
    bar = convert("daily", [row], day)[0]
    basic = convert("daily_basic", [row], day)[0]
    assert bar.volume == 12_300 and bar.amount == 456_000
    assert basic.turnover_rate == pytest.approx(0.052) and basic.total_mv == 1_000_000
    with pytest.raises(ValueError, match="date mismatch"):
        convert("daily", [row], date(2026, 10, 8))
    with pytest.raises(ValueError, match="duplicated"):
        convert("daily", [row, row], day)
    with pytest.raises(ValueError):
        convert("daily", [row | {"close": float("nan")}], day)


def test_provider_failure_cached_and_does_not_leak_token(volume):
    class Client:
        calls = 0

        def query(self, *args, **kwargs):
            self.calls += 1
            raise RuntimeError("SECRET-provider-token")

    client = Client()
    fetcher = Fetcher(volume, datetime(2026, 10, 8, 8, tzinfo=SHANGHAI), client)
    for _ in range(2):
        with pytest.raises(ValueError):
            fetcher.fetch("trade_cal", exchange="SSE")
    assert client.calls == 1
    assert (
        "SECRET-provider-token" not in next((volume / "source_updates").rglob("*.json")).read_text()
    )


@pytest.mark.parametrize("repeat", [False, True])
def test_pagination_bounded_and_duplicate_pages_rejected(volume, monkeypatch, repeat):
    from quantlab.scout.cloud_data import CAPS

    monkeypatch.setitem(CAPS, "daily", 2)

    class Client:
        def query(self, api, **params):
            offset = params["offset"]
            codes = ["000001.SZ", "000002.SZ"] if offset == 0 or repeat else ["000003.SZ"]
            return pd.DataFrame([{"ts_code": c, "trade_date": "20260930"} for c in codes])

    fetcher = Fetcher(volume, datetime(2026, 10, 8, 8, tzinfo=SHANGHAI), Client())
    if repeat:
        with pytest.raises(ValueError, match="pagination ignored"):
            fetcher.all_rows("daily", trade_date="20260930")
    else:
        assert len(fetcher.all_rows("daily", trade_date="20260930")) == 3
        assert fetcher.calls == 2


class FixtureClient:
    def query(self, api, **params):
        if api == "trade_cal":
            return pd.DataFrame(
                [
                    {
                        "exchange": "SSE",
                        "cal_date": d.strftime("%Y%m%d"),
                        "is_open": int(
                            d.weekday() < 5
                            and not date(2026, 10, 1) <= d.date() <= date(2026, 10, 7)
                        ),
                    }
                    for d in pd.date_range("2026-08-01", "2026-10-20")
                ]
            )
        if api == "stock_basic":
            return pd.DataFrame(
                [
                    {
                        "ts_code": "000001.SZ",
                        "symbol": "000001",
                        "name": "测试股",
                        "exchange": "SZSE",
                        "market": "主板",
                        "list_status": "L",
                        "list_date": "19900101",
                        "delist_date": None,
                    }
                ]
                if params["list_status"] == "L"
                else []
            )
        return pd.DataFrame(
            [
                {
                    "ts_code": "000001.SZ",
                    "trade_date": params["trade_date"],
                    "open": 10,
                    "high": 11,
                    "low": 9,
                    "close": 10,
                    "pre_close": 10,
                    "vol": 123,
                    "amount": 456,
                    "turnover_rate": 5,
                    "total_mv": 1000,
                    "circ_mv": 900,
                    "adj_factor": 1,
                    "up_limit": 11,
                    "down_limit": 9,
                    "exchange": "SZSE",
                }
            ]
        )


def test_bootstrap_21_session_window_and_incremental_reuse(volume):
    now = datetime(2026, 10, 8, 8, tzinfo=SHANGHAI)
    fetcher = Fetcher(volume, now, FixtureClient())
    refresh_calendar(volume, fetcher, now)
    result = refresh_market(volume, fetcher, now)
    assert result["asof_session"] == "2026-09-30" and fetcher.calls == 48
    hashes = {
        str(p): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (volume / "market").rglob("*.parquet")
    }
    refresh_market(volume, fetcher, now)
    assert fetcher.calls == 48
    assert hashes == {
        str(p): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (volume / "market").rglob("*.parquet")
    }


def test_scheduler_single_target_duplicate_and_late_data_never_repays(volume, monkeypatch):
    now = datetime(2026, 10, 8, 8, tzinfo=SHANGHAI)
    settings = {"canonical_dir": str(volume / "market"), "release_root": "sealed-app"}
    monkeypatch.setenv("SCOUT_PUBLIC_URL", "https://scout.example.com")
    monkeypatch.delenv("SERVERCHAN_SENDKEY", raising=False)
    monkeypatch.setattr(runner, "prediction_settings", lambda value: value)
    folder = saved_run(volume)
    original_read = runner.read
    monkeypatch.setattr(
        runner,
        "read",
        lambda path: (
            {} if str(path) == "sealed-app/config/scout_daily.fixed.json" else original_read(path)
        ),
    )
    state_path = volume / "runtime/jobs/single/state.json"
    atomic(
        state_path,
        {
            "status": "claimed",
            "key": "single",
            "timing": {"target_session": "2026-10-08"},
            "events": [],
        },
    )
    monkeypatch.setattr(runner, "claim", lambda _: (state_path, read(state_path), True))
    calls = []

    def execute(*args):
        calls.append(1)
        atomic(
            state_path,
            read(state_path) | {"status": "completed", "report_json": str(folder / "report.json")},
        )

    options = {
        "enabled": True,
        "fetcher_factory": lambda r, n: Fetcher(r, n, FixtureClient()),
        "execute": execute,
        "clock": lambda: now,
    }
    assert runner.tick(settings, now, **options)["status"] == "published"
    assert runner.tick(settings, now, **options)["status"] == "published"
    assert len(calls) == 1
    assert (
        runner.tick(settings, now.replace(hour=10), **options)["status"] == "outside_morning_window"
    )
    assert runner.tick(settings, now, enabled=False)["status"] == "schedule_disabled"


def test_scheduler_holiday_and_missed_data_window_never_start_model(volume, monkeypatch):
    settings = {"canonical_dir": str(volume / "market"), "release_root": "sealed-app"}
    monkeypatch.setenv("SCOUT_PUBLIC_URL", "https://scout.example.com")
    monkeypatch.setattr(runner, "prediction_settings", lambda value: value)
    monkeypatch.setattr(runner, "claim", lambda _: pytest.fail("must not claim a model job"))
    options = {
        "enabled": True,
        "fetcher_factory": lambda r, n: Fetcher(r, n, FixtureClient()),
        "execute": lambda *_: pytest.fail("must not call a model"),
    }
    holiday = datetime(2026, 10, 5, 8, tzinfo=SHANGHAI)
    assert runner.tick(settings, holiday, **options)["status"] == "non_trading_day"
    trading = datetime(2026, 10, 8, 8, tzinfo=SHANGHAI)
    result = runner.tick(settings, trading, clock=lambda: trading.replace(hour=9), **options)
    assert result["status"] == "failed"


def test_stop_requested_during_data_preparation_does_not_start_paid_worker(volume, monkeypatch):
    now = datetime(2026, 10, 8, 8, tzinfo=SHANGHAI)
    settings = {"canonical_dir": str(volume / "market"), "release_root": "sealed-app"}
    monkeypatch.setenv("SCOUT_PUBLIC_URL", "https://scout.example.com")
    monkeypatch.delenv("SERVERCHAN_SENDKEY", raising=False)
    monkeypatch.setattr(runner, "prediction_settings", lambda value: value)
    monkeypatch.setattr(runner, "claim", lambda _: pytest.fail("must not claim after stopping"))
    result = runner.tick(
        settings,
        now,
        enabled=True,
        cancelled=lambda: True,
        fetcher_factory=lambda r, n: Fetcher(r, n, FixtureClient()),
    )
    assert result["status"] == "failed"


def test_transient_data_failure_recovers_with_bounded_reads_and_preserved_receipts(volume):
    class Client:
        calls = 0

        def query(self, *args, **kwargs):
            self.calls += 1
            if self.calls < 3:
                raise TimeoutError("SECRET-transient-token")
            return pd.DataFrame([{"exchange": "SSE", "cal_date": "20261008", "is_open": 1}])

    client, waits = Client(), []
    fetcher = Fetcher(
        volume, datetime(2026, 10, 8, 8, tzinfo=SHANGHAI), client, retry_wait=waits.append
    )
    rows = fetcher.fetch("trade_cal", exchange="SSE")
    assert rows[0]["is_open"] == 1 and client.calls == fetcher.calls == 3
    assert waits == [5, 15]
    receipts = sorted((volume / "source_updates").rglob("*.attempt-*.json"))
    assert [read(p)["status"] for p in receipts] == ["failed", "failed", "ok"]
    assert not any(
        "SECRET-transient-token" in p.read_text()
        for p in (volume / "source_updates").rglob("*.json")
    )
    assert fetcher.fetch("trade_cal", exchange="SSE") == rows and client.calls == 3


def test_transient_data_failure_stops_after_three_and_is_not_retried_on_restart(volume):
    class Client:
        calls = 0

        def query(self, *args, **kwargs):
            self.calls += 1
            raise TimeoutError("not-a-real-provider")

    client = Client()
    for _ in range(2):
        fetcher = Fetcher(
            volume,
            datetime(2026, 10, 8, 8, tzinfo=SHANGHAI),
            client,
            retry_wait=lambda seconds: None,
        )
        with pytest.raises(ValueError):
            fetcher.fetch("trade_cal", exchange="SSE")
    assert client.calls == 3
    assert len(list((volume / "source_updates").rglob("*.attempt-*.json"))) == 3


def test_data_recovery_keeps_global_call_budget(volume):
    class Client:
        calls = 0

        def query(self, *args, **kwargs):
            self.calls += 1
            raise TimeoutError("not-a-real-provider")

    client = Client()
    fetcher = Fetcher(
        volume, datetime(2026, 10, 8, 8, tzinfo=SHANGHAI), client, retry_wait=lambda seconds: None
    )
    fetcher.calls = 98
    with pytest.raises(ValueError, match="budget"):
        fetcher.fetch("trade_cal", exchange="SSE")
    assert fetcher.calls == 100 and client.calls == 2


def test_data_transport_retry_classification_is_explicit():
    from urllib.error import HTTPError

    from quantlab.scout.cloud_data import transient_transport_failure

    for status in (429, 500, 502, 503, 504):
        assert transient_transport_failure(
            HTTPError("https://example.invalid", status, "fake", {}, None)
        )
    for status in (400, 401, 403, 404):
        assert not transient_transport_failure(
            HTTPError("https://example.invalid", status, "fake", {}, None)
        )
    assert not transient_transport_failure(ValueError("invalid prices or schema"))
    assert not transient_transport_failure(RuntimeError("credentials or unknown provider failure"))


def test_data_integrity_errors_are_not_retried_or_cached_as_facts(volume):
    class Client:
        calls = 0

        def query(self, *args, **kwargs):
            self.calls += 1
            return pd.DataFrame([{"is_open": 1}, {"is_open": 0}])

    client, waits = Client(), []
    fetcher = Fetcher(
        volume, datetime(2026, 10, 8, 8, tzinfo=SHANGHAI), client, retry_wait=waits.append
    )
    with pytest.raises(ValueError, match="provider request failed"):
        fetcher.fetch("trade_cal", exchange="SSE", limit=1)
    assert client.calls == 1 and waits == []
    for file in (volume / "source_updates").rglob("*.json"):
        assert read(file)["status"] == "failed" and "rows" not in read(file)
