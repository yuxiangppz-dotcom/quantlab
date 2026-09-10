from dataclasses import replace
from datetime import UTC, date, datetime
from unittest.mock import Mock

import pandas as pd
import pytest

from quantlab.data import index_context as context
from quantlab.data.models import DataValidationError, IndexDailyBar
from quantlab.data.tushare_provider import TushareProvider

HEAD = "a" * 40
NOW = datetime(2026, 9, 11, tzinfo=UTC)
SPEC = context.IndexContextSpec("000688.SH", date(2020, 7, 22), date(2020, 7, 24))


def _metadata(code):
    return [
        {
            "ts_code": code,
            "name": "科创50",
            "fullname": None,
            "market": "SSE",
            "publisher": "中证指数有限公司",
            "base_date": "20191231",
            "list_date": "20200723",
        }
    ]


def _bar(code, day):
    return IndexDailyBar(code, day, 100, 105, 90, 101, 99, 12300, 456000)


@pytest.fixture
def inputs(tmp_path):
    calendar = tmp_path / "calendar.parquet"
    pd.DataFrame(
        [
            {"exchange": "SSE", "trade_date": pd.Timestamp(day), "is_open": True}
            for day in pd.date_range(SPEC.start, SPEC.end)
        ]
    ).to_parquet(calendar, index=False)
    provider = Mock()
    provider.get_index_metadata.side_effect = _metadata
    provider.get_index_daily.side_effect = lambda code, start, end: [
        _bar(code, day.date()) for day in pd.date_range(start, end)
    ]
    return calendar, tmp_path / "supplemental", provider


def _sync(inputs, **kwargs):
    calendar, output, provider = inputs
    return context.sync_index_context(
        lambda: provider,
        calendar,
        output,
        **{"code_head": HEAD, "specs": (SPEC,), "clock": lambda: NOW, **kwargs},
    )


def test_snapshot_publication_provenance_and_reuse(inputs):
    calendar, output, provider = inputs
    legacy = output.parent / "legacy-benchmarks.parquet"
    legacy.write_bytes(b"immutable original benchmark")
    before = calendar.read_bytes(), legacy.read_bytes()
    path, result, reused = _sync(inputs)
    assert not reused and path.parent == output
    assert result["code_head"] == HEAD
    assert result["series"][0]["metadata_observed_at"] == "2026-09-11T08:00:00+08:00"
    assert [row["index_published_on_date"] for row in result["series"][0]["rows"]] == [
        False,
        True,
        True,
    ]
    assert result["request"]["semantics"]["historical_revision_status"] == "unverified"
    assert result["request"]["semantics"]["execution_authority"] is False
    assert context.load_index_context(path, expected_fingerprint=result["fingerprint"]) == result
    untouched = path.read_bytes()
    fail_factory = Mock(side_effect=AssertionError("cached reads must not call provider"))
    second = context.sync_index_context(
        fail_factory,
        calendar,
        output,
        code_head="b" * 40,
        specs=(SPEC,),
        clock=lambda: NOW,
    )
    assert second == (path, result, True)
    assert path.read_bytes() == untouched
    assert before == (calendar.read_bytes(), legacy.read_bytes())
    assert provider.get_index_daily.call_count == 1


@pytest.mark.parametrize(
    "change",
    [
        lambda rows: rows[:-1],
        lambda rows: rows + rows[:1],
        lambda rows: [],
        lambda rows: [replace(row, instrument_id="000001.SH") for row in rows],
        lambda rows: [replace(row, trade_date=date(2020, 7, 25)) for row in rows],
        lambda rows: [replace(row, trade_date=datetime(2020, 7, 23)) for row in rows],
        lambda rows: [replace(row, close=float("nan")) for row in rows],
        lambda rows: [replace(row, high=99) for row in rows],
        lambda rows: [replace(row, low=102) for row in rows],
        lambda rows: [replace(row, open=0) for row in rows],
        lambda rows: [replace(row, pre_close=-1) for row in rows],
        lambda rows: [replace(row, volume=-1) for row in rows],
        lambda rows: [replace(row, amount=float("inf")) for row in rows],
    ],
)
def test_invalid_provider_rows_never_publish(inputs, change):
    provider = inputs[2]
    original = provider.get_index_daily.side_effect
    provider.get_index_daily.side_effect = lambda *args: change(original(*args))
    with pytest.raises(DataValidationError):
        _sync(inputs)
    assert not inputs[1].exists()


def test_optional_missing_values_remain_unknown(inputs):
    provider = inputs[2]
    original = provider.get_index_daily.side_effect
    provider.get_index_daily.side_effect = lambda *args: [
        replace(row, volume=float("nan"), amount=None) for row in original(*args)
    ]
    _, bundle, _ = _sync(inputs)
    for row in bundle["series"][0]["rows"]:
        assert row["volume"] is None and row["amount"] is None


@pytest.mark.parametrize(
    "change",
    [
        lambda frame: frame.iloc[:-1],
        lambda frame: pd.concat([frame, frame.iloc[:1]]),
        lambda frame: frame.assign(is_open=None),
        lambda frame: frame.assign(is_open=2),
        lambda frame: frame.assign(exchange="SZSE"),
        lambda frame: frame.assign(trade_date=frame.trade_date + pd.Timedelta(hours=1)),
    ],
)
def test_invalid_calendar_prevents_provider_calls(inputs, change):
    calendar, _, provider = inputs
    change(pd.read_parquet(calendar)).to_parquet(calendar, index=False)
    with pytest.raises(DataValidationError):
        _sync(inputs)
    provider.get_index_metadata.assert_not_called()


@pytest.mark.parametrize(
    "change",
    [
        lambda rows: [],
        lambda rows: rows + rows,
        lambda rows: [{**rows[0], "ts_code": "000001.SH"}],
        lambda rows: [{**rows[0], "market": "CSI"}],
        lambda rows: [{**rows[0], "name": None}],
        lambda rows: [{**rows[0], "list_date": "20200724"}],
        lambda rows: [{**rows[0], "base_date": "20200723"}],
    ],
)
def test_unverified_identity_never_requests_daily(inputs, change):
    provider = inputs[2]
    provider.get_index_metadata.side_effect = lambda code: change(_metadata(code))
    with pytest.raises(DataValidationError):
        _sync(inputs)
    provider.get_index_daily.assert_not_called()


def test_tampering_and_changed_fingerprint_are_rejected_without_provider(inputs):
    path, bundle, _ = _sync(inputs)
    with pytest.raises(DataValidationError, match="fingerprint"):
        context.load_index_context(path, expected_fingerprint="0" * 64)
    bundle["series"][0]["rows"][0]["close"] = 102
    path.write_bytes(context._bytes(bundle))
    inputs[2].reset_mock()
    with pytest.raises(DataValidationError, match="fingerprint"):
        _sync(inputs)
    inputs[2].get_index_metadata.assert_not_called()


@pytest.mark.parametrize("part", ["publication", "receipt", "time", "authority", "code"])
def test_rehashed_invalid_semantics_rejected(inputs, part):
    path, bundle, _ = _sync(inputs)
    if part == "publication":
        bundle["series"][0]["rows"][0]["index_published_on_date"] = True
    elif part == "receipt":
        bundle["series"][0]["requests"][0]["row_count"] = 99
    elif part == "time":
        bundle["series"][0]["metadata_observed_at"] = "2020-01-01T00:00:00"
    elif part == "authority":
        bundle["request"]["semantics"]["execution_authority"] = True
    else:
        bundle["code_head"] = "unknown"
    bundle.pop("fingerprint")
    bundle["fingerprint"] = context._hash(bundle)
    path.write_bytes(context._bytes(bundle))
    with pytest.raises(DataValidationError):
        context.load_index_context(path)


def test_calendar_drift_during_fetch_does_not_publish(inputs):
    calendar, output, provider = inputs
    original = provider.get_index_daily.side_effect

    def changing(*args):
        calendar.write_bytes(b"changed by another process")
        return original(*args)

    provider.get_index_daily.side_effect = changing
    with pytest.raises(DataValidationError, match="calendar changed"):
        _sync(inputs)
    assert not output.exists()


def test_current_day_and_bad_source_head_reject_before_calls(inputs):
    with pytest.raises(DataValidationError, match="prior dates"):
        _sync(inputs, clock=lambda: datetime(2020, 7, 24, tzinfo=UTC))
    with pytest.raises(DataValidationError, match="commit"):
        _sync(inputs, code_head="short")
    with pytest.raises(DataValidationError, match="timezone"):
        _sync(inputs, clock=lambda: datetime(2026, 9, 11))
    inputs[2].get_index_metadata.assert_not_called()


def test_year_chunks_bound_requests(inputs):
    calendar, _, provider = inputs
    spec = replace(SPEC, start=date(2020, 12, 30), end=date(2021, 1, 4))
    pd.DataFrame(
        [
            {"exchange": "SSE", "trade_date": day, "is_open": True}
            for day in pd.date_range(spec.start, spec.end)
        ]
    ).to_parquet(calendar, index=False)
    _, bundle, _ = _sync(inputs, specs=(spec,))
    assert len(bundle["series"][0]["rows"]) == 6
    assert provider.get_index_daily.call_args_list[0].args == (
        "000688.SH",
        date(2020, 12, 30),
        date(2020, 12, 31),
    )
    assert provider.get_index_daily.call_args_list[1].args == (
        "000688.SH",
        date(2021, 1, 1),
        date(2021, 1, 4),
    )


def test_metadata_provider_is_bounded_and_keeps_response(monkeypatch):
    import quantlab.data.tushare_provider as module

    pro = Mock()
    pro.index_basic.return_value = pd.DataFrame(_metadata("000688.SH"))
    monkeypatch.setattr(module.ts, "pro_api", lambda token: pro)
    assert TushareProvider(token="synthetic").get_index_metadata("000688.SH") == _metadata(
        "000688.SH"
    )
    assert pro.index_basic.call_args.kwargs["ts_code"] == "000688.SH"
    assert pro.index_basic.call_args.kwargs["market"] == "SSE"
