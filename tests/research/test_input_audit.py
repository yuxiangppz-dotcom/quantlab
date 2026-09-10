import hashlib
import json
from datetime import date

import pandas as pd
import pytest

from quantlab.data.storage import ParquetStorage
from quantlab.research import input_audit as module
from quantlab.research.input_audit import audit_research_inputs


def _seed(tmp_path):
    storage = ParquetStorage(tmp_path / "canonical")
    days = [date(2020, 7, day) for day in (22, 23, 24)]

    def write(path, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(rows).to_parquet(path, index=False)

    write(
        storage.calendar_path,
        [
            {"exchange": exchange, "trade_date": pd.Timestamp(day), "is_open": True}
            for day in days
            for exchange in ("SSE", "SZSE")
        ],
    )
    write(
        storage.securities_path,
        [
            {
                "instrument_id": "000001.SZ",
                "list_date": pd.Timestamp("2000-01-01"),
                "delist_date": None,
                "board": "主板",
            }
        ],
    )
    for day in days:
        base = {"instrument_id": "000001.SZ", "trade_date": pd.Timestamp(day)}
        write(
            storage.daily_bars_path(day),
            [
                {
                    **base,
                    "open": 10.0,
                    "high": 11.0,
                    "low": 9.0,
                    "close": 10.0,
                    "volume": 1000.0,
                    "amount": 10000.0,
                }
            ],
        )
        write(storage.adj_factor_path(day), [{**base, "adj_factor": 1.0}])
        write(
            storage.daily_basic_path(day),
            [{**base, "turnover_rate": 0.01, "circ_mv": 10000.0, "total_mv": 20000.0}],
        )
        write(
            storage.index_daily_path(day),
            [
                {
                    "instrument_id": index,
                    "trade_date": pd.Timestamp(day),
                    "open": 1000.0,
                    "high": 1001.0,
                    "low": 999.0,
                    "close": 1000.0,
                }
                for index, available in module.INDEX_AVAILABLE_FROM.items()
                if day >= available
            ],
        )
        for path in (storage.stock_st_v1_path(day), storage.suspensions_v1_path(day)):
            path.parent.mkdir(parents=True, exist_ok=True)
            pd.DataFrame(columns=["instrument_id", "trade_date", "source_record_id"]).to_parquet(
                path, index=False
            )
        write(storage.daily_price_limit_path(day), [base])
    changes = tmp_path / "changes.csv"
    pd.DataFrame(
        columns=["old_instrument_id", "new_instrument_id", "effective_date", "original_list_date"]
    ).to_csv(changes, index=False)
    return storage, days, changes


def _audit(storage, days, changes, **kw):
    return audit_research_inputs(
        storage, days[0], days[-1], warmup_sessions=0, code_changes_path=changes, **kw
    )


def _tree(root):
    return {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_consistent_rows_still_do_not_certify_publication_or_tradability(tmp_path):
    storage, days, changes = _seed(tmp_path)
    before = _tree(tmp_path)
    result = _audit(storage, days, changes, code_head="a" * 40)
    assert result["status"] == "locally_consistent_with_limitations"
    assert result["target_session_count"] == 3
    assert result["totals"]["daily"]["rows"] == 3
    assert result["totals"]["stock_st"]["rows"] == 0
    assert len(result["source_manifest"]) == 24
    assert not result["performance_eligible"] and not result["execution_authority"]
    assert result["index_available_from"]["000688.SH"] == "2020-07-23"
    core = {key: value for key, value in result.items() if key != "content_fingerprint"}
    assert (
        result["content_fingerprint"]
        == hashlib.sha256(
            json.dumps(core, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    assert _tree(tmp_path) == before


@pytest.mark.parametrize(
    "mutation,code",
    [
        ("duplicate", "invalid_or_duplicate_keys"),
        ("date", "wrong_partition_date"),
        ("null_key", "invalid_or_duplicate_keys"),
        ("ohlc", "invalid_numeric_rows"),
        ("missing_column", "missing_columns"),
        ("empty", "empty_required_table"),
    ],
)
def test_bad_daily_rows_are_visible(tmp_path, mutation, code):
    storage, days, changes = _seed(tmp_path)
    path = storage.daily_bars_path(days[1])
    frame = pd.read_parquet(path)
    if mutation == "duplicate":
        frame = pd.concat([frame, frame], ignore_index=True)
    elif mutation == "date":
        frame["trade_date"] = pd.Timestamp(days[0])
    elif mutation == "null_key":
        frame["instrument_id"] = None
    elif mutation == "ohlc":
        frame["high"] = 8.0
    elif mutation == "missing_column":
        frame = frame.drop(columns="close")
    else:
        frame = frame.iloc[0:0]
    frame.to_parquet(path, index=False)
    result = _audit(storage, days, changes)
    assert any(row["code"] == code for row in result["findings"])
    assert result["status"] == "needs_input_work"


def test_missing_inputs_and_join_loss_are_retained(tmp_path):
    storage, days, changes = _seed(tmp_path)
    storage.daily_bars_path(days[0]).unlink()
    storage.suspensions_v1_path(days[1]).unlink()
    path = storage.adj_factor_path(days[2])
    frame = pd.read_parquet(path)
    frame["instrument_id"] = "999999.SZ"
    frame.to_parquet(path, index=False)
    result = _audit(storage, days, changes)
    codes = {row["code"] for row in result["findings"]}
    assert {"missing_file", "missing_context_partitions", "unmatched_daily_keys"} <= codes


@pytest.mark.parametrize(
    "mutation,code",
    [("missing", "unverified_calendar_days"), ("conflict", "exchange_calendar_conflict")],
)
def test_calendar_missing_exchange_and_conflicting_sessions_are_not_inferred(
    tmp_path, mutation, code
):
    storage, days, changes = _seed(tmp_path)
    frame = pd.read_parquet(storage.calendar_path)
    if mutation == "missing":
        frame = frame.iloc[1:]
    else:
        frame.loc[0, "is_open"] = False
    frame.to_parquet(storage.calendar_path, index=False)
    assert any(row["code"] == code for row in _audit(storage, days, changes)["findings"])


def test_missing_star_index_is_required_only_after_publication(tmp_path):
    storage, days, changes = _seed(tmp_path)
    for day in days:
        path = storage.index_daily_path(day)
        frame = pd.read_parquet(path)
        frame[frame["instrument_id"] != "000688.SH"].to_parquet(path, index=False)
    result = _audit(storage, days, changes)
    missing = [
        row for row in result["findings"] if row["code"] == "missing_published_index_sessions"
    ]
    assert missing[0]["dates"] == ["2020-07-23", "2020-07-24"]


def test_source_drift_is_reported_instead_of_certifying_the_original_read(tmp_path, monkeypatch):
    storage, days, changes = _seed(tmp_path)
    original = module._sha

    def changed(path):
        value = original(path)
        return "0" * 64 if path == storage.daily_bars_path(days[0]) else value

    monkeypatch.setattr(module, "_sha", changed)
    assert any(
        row["code"] == "source_changed_during_audit"
        for row in _audit(storage, days, changes)["findings"]
    )


def test_insufficient_warmup_is_not_silently_shortened(tmp_path):
    storage, days, changes = _seed(tmp_path)
    result = audit_research_inputs(
        storage, days[0], days[-1], warmup_sessions=120, code_changes_path=changes
    )
    assert any(row["code"] == "insufficient_calendar_warmup" for row in result["findings"])


@pytest.mark.parametrize(
    "dataset,column", [("adj_factor", "adj_factor"), ("daily_basic", "circ_mv")]
)
def test_nonpositive_factor_inputs_are_not_eligible_for_log_or_adjusted_prices(
    tmp_path, dataset, column
):
    storage, days, changes = _seed(tmp_path)
    path = getattr(storage, f"{dataset}_path")(days[0])
    frame = pd.read_parquet(path)
    frame[column] = 0.0
    frame.to_parquet(path, index=False)
    findings = _audit(storage, days, changes)["findings"]
    assert any(
        row["code"] == "invalid_numeric_rows" and row["dataset"] == dataset for row in findings
    )
