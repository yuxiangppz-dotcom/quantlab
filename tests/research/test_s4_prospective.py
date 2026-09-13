import copy
import hashlib
import json
import time
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from quantlab.data.models import DataValidationError
from quantlab.data.program_intake import Journal, acquire
from quantlab.research import s4_prospective as m

DAYS = list(pd.bdate_range("2026-08-14", "2026-09-11").strftime("%Y-%m-%d"))
CODES = ["000001.SZ", "600000.SH"]
ROW = ["000001.SZ", "20260911", 10, 11, 9, 10.5, 10, 20, 30]


def wire(row=ROW, *, api="daily", items=None, code=0, extra=None):
    fields = m.DAILY_FIELDS if api == "daily" else m.ADJ_FIELDS
    p = {
        "code": code,
        "data": {"fields": fields.split(","), "items": [row] if items is None else items},
    }
    p["data"].update(extra or {})
    return json.dumps(p).encode()


def fixture_frame():
    return pd.DataFrame(
        [
            {
                "instrument_id": code,
                "trade_date": pd.Timestamp(day),
                "open": 10 + j * step,
                "high": 30,
                "low": 1,
                "close": 10 + j * step,
                "volume": 100,
                "amount": 1000,
                "adj_factor": 2.0,
            }
            for code, step in zip(CODES, (0.1, -0.1), strict=True)
            for j, day in enumerate(DAYS)
        ]
    )


def test_formula_and_adjustment_factor_use_only_fixed_trailing_windows():
    grid, scores = m.score_observation(fixture_frame(), DAYS, CODES)
    expected3 = [12 / 11.7 - 1, 8 / 8.3 - 1]
    np.testing.assert_allclose(scores.change3, expected3)
    np.testing.assert_allclose(scores["S4-A"], np.median(expected3) - np.array(expected3))
    np.testing.assert_allclose(scores.reference_reversal20, [-0.2, 0.2])
    assert len(grid) == 42 and len(scores) == 2
    assert not {"label", "return", "weight", "order", "nav"}.intersection(scores)


@pytest.mark.parametrize(
    "position,known3", [(0, True), (16, True), (17, False), (19, False), (20, False)]
)
def test_missing_sessions_are_not_compressed_or_survivor_dropped(position, known3):
    frame = fixture_frame().drop(index=position)
    grid, scores = m.score_observation(frame, DAYS, CODES)
    assert len(grid) == 42 and list(scores.instrument_id) == CODES
    assert pd.notna(scores.iloc[0]["S4-A"]) == known3
    assert pd.isna(scores.iloc[0].reference_reversal20)


@pytest.mark.parametrize(
    "field,value",
    [
        ("open", 0),
        ("high", 1),
        ("low", 40),
        ("close", -1),
        ("volume", 0),
        ("amount", np.nan),
        ("adj_factor", np.inf),
    ],
)
def test_invalid_latest_bar_keeps_unknown_score(field, value):
    frame = fixture_frame()
    frame.loc[20, field] = value
    _, scores = m.score_observation(frame, DAYS, CODES)
    assert pd.isna(scores.iloc[0]["S4-A"]) and pd.isna(scores.iloc[0].reference_reversal20)


@pytest.mark.parametrize("defect", ["future", "duplicate", "foreign_code", "bad_window", "boolean"])
def test_input_grid_and_numeric_types_reject_leakage(defect):
    frame, days = fixture_frame(), DAYS.copy()
    if defect == "future":
        frame.loc[0, "trade_date"] = pd.Timestamp("2026-09-14")
    elif defect == "duplicate":
        frame = pd.concat([frame, frame.iloc[:1]])
    elif defect == "foreign_code":
        frame.loc[0, "instrument_id"] = "000002.SZ"
    elif defect == "bad_window":
        days = days[1:]
    else:
        frame["close"] = True
    with pytest.raises(DataValidationError):
        m.score_observation(frame, days, CODES)


def test_response_units_nulls_and_api_scope(tmp_path):
    assert [r["parameters"]["api_name"] for r in m.requests_for()] == ["daily", "adj_factor"]
    assert all(r["parameters"]["params"] == {"trade_date": "20260911"} for r in m.requests_for())
    p = tmp_path / "raw"
    p.write_bytes(wire())
    f = m._wire_frame(p, "daily").iloc[0]
    assert f.volume == 2000 and f.amount == 30000 and f.close == 10.5
    row = ROW.copy()
    row[-1] = None
    p.write_bytes(wire(row))
    assert m.profile(p.read_bytes(), m.requests_for()[0])["status"] == "nonempty"
    assert pd.isna(m._wire_frame(p, "daily").iloc[0].amount)


@pytest.mark.parametrize("value", [True, False, "10.5", "", [], {}, float("inf"), float("nan")])
def test_raw_numeric_schema_cannot_convert_unknown_types_into_prices(value):
    row = ROW.copy()
    row[5] = value
    assert m.profile(wire(row), m.requests_for()[0])["status"] == "schema_error"


@pytest.mark.parametrize(
    "defect,status",
    [
        ("empty", "empty_response_terminal"),
        ("duplicate", "schema_error"),
        ("wrong_date", "schema_error"),
        ("wrong_code", "schema_error"),
        ("error", "provider_error"),
        ("more", "saturated"),
        ("total", "saturated"),
        ("cap", "saturated"),
        ("bad_json", "schema_error"),
    ],
)
def test_terminal_response_profiles(defect, status):
    row, request = ROW.copy(), m.requests_for()[0]
    if defect == "wrong_date":
        row[1] = "20260914"
    if defect == "wrong_code":
        row[0] = "wrong"
    raw = wire(row, code=1 if defect == "error" else 0)
    if defect == "empty":
        raw = wire(items=[])
    if defect == "duplicate":
        raw = wire(items=[row, row])
    if defect in ("more", "total"):
        raw = wire(extra={"has_more": True} if defect == "more" else {"total": 2})
    if defect == "cap":
        request["row_cap"] = 1
    if defect == "bad_json":
        raw = b"{"
    assert m.profile(raw, request)["status"] == status


def calendar():
    return pd.DataFrame(
        [
            {"exchange": ex, "trade_date": day, "is_open": day.weekday() < 5}
            for ex in ("SSE", "SZSE")
            for day in pd.date_range("2026-08-14", "2026-09-21")
        ]
    )


def test_exchange_calendar_has_fixed_future_reference_window():
    window, future = m.calendar_window(calendar())
    assert window == DAYS and len(future) == 6
    assert future[0] == "2026-09-14" and future[-1] == "2026-09-21"


@pytest.mark.parametrize("defect", ["gap", "disagree", "duplicate", "unknown", "int_state"])
def test_calendar_uncertainty_fails(defect):
    f = calendar()
    if defect == "gap":
        f = f.drop(index=0)
    elif defect == "disagree":
        f.loc[0, "is_open"] = False
    elif defect == "duplicate":
        f = pd.concat([f, f.iloc[:1]])
    elif defect == "unknown":
        f["is_open"] = f.is_open.astype("boolean")
        f.loc[0, "is_open"] = pd.NA
    else:
        f["is_open"] = f.is_open.astype(int)
    with pytest.raises(DataValidationError):
        m.calendar_window(f)


def test_weekend_timestamp_is_not_backdated_forward_shadow():
    now = datetime(2026, 9, 13, 1, tzinfo=UTC)
    result = m.timing(now, now - timedelta(seconds=10))
    assert result["created_at"] == now.isoformat()
    assert result["old_same_day_forward_shadow_eligible"] is False
    assert result["future_window_not_started"] is True


@pytest.mark.parametrize(
    "created,source",
    [
        (datetime(2026, 9, 13), datetime(2026, 9, 13)),
        (datetime(2026, 9, 11, 8, tzinfo=UTC), datetime(2026, 9, 11, 8, tzinfo=UTC)),
        (datetime(2026, 9, 13, 1, tzinfo=UTC), datetime(2026, 9, 13, 2, tzinfo=UTC)),
        (datetime(2026, 9, 13, 1, tzinfo=UTC), datetime(2026, 9, 11, 7, tzinfo=UTC)),
        (datetime(2026, 9, 14, 1, 30, tzinfo=UTC), datetime(2026, 9, 13, 1, tzinfo=UTC)),
        (datetime(2026, 9, 14, 1, 31, tzinfo=UTC), datetime(2026, 9, 13, 1, tzinfo=UTC)),
    ],
)
def test_no_stale_naive_retroactive_or_late_publication(created, source):
    with pytest.raises(DataValidationError):
        m.timing(created, source)


@pytest.mark.parametrize("name", ["started.json", "failed.json", "scores.parquet", "attempts"])
def test_any_prior_work_consumes_actual_attempt(tmp_path, name):
    (tmp_path / "worker.lock").touch()
    m.require_unused(tmp_path)
    (tmp_path / name).touch()
    with pytest.raises(DataValidationError, match="consumed"):
        m.require_unused(tmp_path)


def config():
    return json.loads((Path(__file__).resolve().parents[2] / m.CONFIG).read_text())


@pytest.mark.parametrize(
    "defect", ["endpoint", "unbound", "extra", "bytes", "resource", "path", "boolean_cap"]
)
def test_contract_rejects_unbound_inputs_or_expansion(defect):
    c = copy.deepcopy(config())
    m.validate_config(c)
    if defect == "endpoint":
        c["endpoint"] = "http://api.tushare.pro"
    elif defect == "unbound":
        del c["inputs"][c["canonical_paths"][0]]
    elif defect == "extra":
        c["inputs"]["extra"] = next(iter(c["inputs"].values()))
    elif defect == "bytes":
        c["inputs"][c["calendar_path"]]["bytes"] = True
    elif defect == "resource":
        c["resources"]["max_wakeup_seconds"] = 1000
    elif defect == "path":
        c["calendar_path"] = "unbound.parquet"
    else:
        c["max_actual_attempts"] = True
    with pytest.raises(DataValidationError):
        m.validate_config(c)


class TestBudget:
    __test__ = False

    def __init__(self, *args):
        self.started = time.monotonic()

    def check(self, **kwargs):
        return 0

    def can_start(self):
        return True

    def watchdog(self):
        return nullcontext()


@pytest.mark.parametrize("empty", [False, True])
def test_real_journal_pipeline_has_two_calls_or_stops_on_first_empty_without_retry(tmp_path, empty):
    c = config()
    journal = Journal(tmp_path, c, m.requests_for(), "synthetic", inspector=m.profile)
    calls = []

    class Client:
        def fetch(self, request, cap):
            calls.append(request)
            api = request["api_name"]
            raw = wire(
                items=[] if empty else None,
                api=api,
                row=ROW if api == "daily" else ["000001.SZ", "20260911", 2.0],
            )
            return raw, {
                "transport_status": "received",
                "http_status": 200,
                "received_bytes": len(raw),
                "wire_sha256": hashlib.sha256(raw).hexdigest(),
                "raw_redacted": False,
                "body_count_complete": True,
                "budget_body_bytes": len(raw),
            }

    stop = acquire(journal, TestBudget(), Client(), gap_seconds=0)
    assert len(calls) == (1 if empty else 2)
    assert stop == ("empty_response_terminal" if empty else "request_scope_exhausted")
    assert journal.summary()["execution_authority"] is False
    if empty:
        with pytest.raises(DataValidationError, match="must not be retried"):
            journal.next_request()


@pytest.mark.parametrize("mode", ["success", "empty", "client_error"])
def test_whole_worker_seals_one_attempt_and_never_grants_trading_authority(
    tmp_path, monkeypatch, mode
):
    c = config()
    c["inputs"] = {}
    f = fixture_frame()
    # Deliberately mix Timestamp canonical dates and Python-date wire conversion.
    for path in c["canonical_paths"]:
        day = pd.Timestamp(Path(path).stem)
        part = f[f.trade_date.eq(day)]
        measures = (
            ["adj_factor"]
            if "/adj_factor/" in path
            else [
                "open",
                "high",
                "low",
                "close",
                "volume",
                "amount",
            ]
        )
        dest = tmp_path / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        part[[*m.KEYS, *measures]].to_parquet(dest, index=False)
        c["inputs"][path] = {"sha256": m._sha(dest), "bytes": dest.stat().st_size}
    config_path = tmp_path / m.CONFIG
    config_path.parent.mkdir(parents=True)
    config_path.write_text(json.dumps(c))

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 13, 1, tzinfo=UTC).astimezone(tz or UTC)

    monkeypatch.setattr(m, "datetime", FrozenDatetime)
    monkeypatch.setattr("quantlab.data.program_intake.now", lambda: "2026-09-13T01:00:00+00:00")
    monkeypatch.setattr(m, "load_contract", lambda root: (c, CODES))
    monkeypatch.setattr(m, "Budget", TestBudget)
    monkeypatch.setattr(m.subprocess, "check_output", lambda *a, **k: "synthetic-head")
    monkeypatch.setattr(m, "code_binding", lambda root, binding: "synthetic-head")
    monkeypatch.setattr(
        m,
        "acquire",
        lambda journal, budget, client, **k: acquire(journal, budget, client, gap_seconds=0),
    )
    monkeypatch.setenv("TUSHARE_TOKEN", "unit-test-placeholder")
    calls, closed = [], []

    class Client:
        def __init__(self, token, endpoint):
            if mode == "client_error":
                raise RuntimeError("synthetic constructor failure")

        def fetch(self, request, cap):
            calls.append(request)
            api = request["api_name"]
            raw = wire(
                items=[] if mode == "empty" else None,
                api=api,
                row=ROW if api == "daily" else ["000001.SZ", "20260911", 2.0],
            )
            return raw, {
                "transport_status": "received",
                "http_status": 200,
                "received_bytes": len(raw),
                "wire_sha256": hashlib.sha256(raw).hexdigest(),
                "raw_redacted": False,
                "body_count_complete": True,
                "budget_body_bytes": len(raw),
            }

        def close(self):
            closed.append(True)

    out = tmp_path / m.OUTPUT
    if mode == "success":
        report = m.run(tmp_path, client_factory=Client)
        assert report["rows"] == 2 and report["known_S4_A"] == 1
        assert report["known_reference20"] == 1 and report["future_diagnostic_pending"] is True
        assert all(
            report[k] is False
            for k in (
                "broker_order",
                "performance_evidence",
                "execution_authority",
                "candidate_promoted",
            )
        )
        assert report["economic_paths"] == report["model_fits"] == 0
    else:
        with pytest.raises((DataValidationError, RuntimeError)):
            m.run(tmp_path, client_factory=Client)
        assert (out / "failed.json").exists() and not (out / "observation.json").exists()
    expected_calls = {"success": 2, "empty": 1, "client_error": 0}[mode]
    assert len(calls) == expected_calls
    assert bool(closed) == (mode != "client_error")
    with pytest.raises(DataValidationError, match="consumed"):
        m.run(tmp_path, client_factory=Client)
    assert len(calls) == expected_calls
