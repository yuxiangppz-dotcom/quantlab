"""Row-audit correctness against the canonical data models (v0.2.1 Part F).

The v0.2 audit treated every expected field as required non-null and used
``instrument_id`` alone as the context-family primary key, so a nullable
``suspend_timing`` counted as an anomaly and legitimate multi-event rows
(counted as false duplicates) polluted the evidence. v0.2.1 defines each
family's own primary key, required non-null fields, and numeric finite
fields from the canonical storage contract.
"""

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from quantlab.execution.readiness import audit_partition_rows

THU = date(2024, 11, 28)
FRI = date(2024, 11, 29)

_DAILY_FIELDS = {
    "instrument_id", "trade_date", "open", "high", "low", "close",
    "pre_close", "volume", "amount",
}
_ST_FIELDS = {
    "instrument_id", "trade_date", "name", "status", "type_name",
    "source_record_id",
}
_SUSPENSION_FIELDS = {
    "instrument_id", "trade_date", "suspend_type", "suspend_timing",
    "source_record_id",
}


def _write_partition(root: Path, family: str, day: date, frame: pd.DataFrame) -> None:
    path = root / family / f"year={day.year}" / f"month={day.month:02d}" / (
        f"{day.isoformat()}.parquet"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(path)


def _base_suspension_frame() -> pd.DataFrame:
    return pd.DataFrame({
        "instrument_id": ["600000.SH", "600000.SH", "000001.SZ"],
        "trade_date": [FRI, FRI, FRI],
        "suspend_type": ["S", "S", "S"],
        "suspend_timing": [None, "open_call_auction", None],
        "source_record_id": ["rec-1", "rec-2", "rec-3"],
    })


def test_nullable_suspend_timing_is_not_an_anomaly(tmp_path: Path) -> None:
    _write_partition(tmp_path, "suspensions", FRI, _base_suspension_frame())
    report = audit_partition_rows(
        tmp_path, "suspensions", [FRI],
        expected_fields=_SUSPENSION_FIELDS, family_kind="suspensions",
    )
    assert report["audited_rows"] == 3
    assert report["null_or_nonfinite_required_fields"] == 0
    assert report["duplicate_primary_keys"] == 0
    assert report["all_rows_clean"] is True


def test_multi_event_suspension_rows_are_not_duplicates(tmp_path: Path) -> None:
    """Same instrument+date with different source_record_ids is legitimate."""
    frame = _base_suspension_frame()
    frame.loc[len(frame)] = [
        "600000.SH", FRI, "S", None, "rec-4",
    ]
    _write_partition(tmp_path, "suspensions", FRI, frame)
    report = audit_partition_rows(
        tmp_path, "suspensions", [FRI],
        expected_fields=_SUSPENSION_FIELDS, family_kind="suspensions",
    )
    assert report["duplicate_primary_keys"] == 0
    assert report["audited_rows"] == 4


def test_true_duplicate_suspension_key_is_flagged(tmp_path: Path) -> None:
    frame = _base_suspension_frame()
    frame.loc[len(frame)] = ["600000.SH", FRI, "S", None, "rec-1"]
    _write_partition(tmp_path, "suspensions", FRI, frame)
    report = audit_partition_rows(
        tmp_path, "suspensions", [FRI],
        expected_fields=_SUSPENSION_FIELDS, family_kind="suspensions",
    )
    assert report["duplicate_primary_keys"] == 1
    assert report["anomaly_samples"]


def test_missing_required_suspension_field_is_flagged(tmp_path: Path) -> None:
    frame = _base_suspension_frame()
    frame.loc[0, "suspend_type"] = None
    _write_partition(tmp_path, "suspensions", FRI, frame)
    report = audit_partition_rows(
        tmp_path, "suspensions", [FRI],
        expected_fields=_SUSPENSION_FIELDS, family_kind="suspensions",
    )
    assert report["null_or_nonfinite_required_fields"] == 1


def test_st_context_nullable_fields_are_not_anomalies(tmp_path: Path) -> None:
    frame = pd.DataFrame({
        "instrument_id": ["600000.SH", "600000.SH"],
        "trade_date": [FRI, FRI],
        "name": ["ST Name A", "ST Name B"],
        "status": [None, None],
        "type_name": [None, None],
        "source_record_id": ["st-1", "st-2"],
    })
    _write_partition(tmp_path, "lifecycle_context_v1/stock_st", FRI, frame)
    report = audit_partition_rows(
        tmp_path, "lifecycle_context_v1/stock_st", [FRI],
        expected_fields=_ST_FIELDS, family_kind="stock_st",
    )
    assert report["null_or_nonfinite_required_fields"] == 0
    assert report["duplicate_primary_keys"] == 0


def test_daily_audit_flags_null_and_ohlc_and_negative(tmp_path: Path) -> None:
    frame = pd.DataFrame({
        "instrument_id": ["600000.SH", "600001.SH", "000001.SZ"],
        "trade_date": [FRI, FRI, FRI],
        "open": [10.0, 10.0, 10.0],
        "high": [11.0, 9.0, 11.0],
        "low": [9.0, 8.0, 12.0],
        "close": [None, 9.5, 10.5],
        "pre_close": [10.0, 10.0, 10.0],
        "volume": [1000.0, 2000.0, -5.0],
        "amount": [1.0e4, 2.0e4, 3.0e4],
    })
    _write_partition(tmp_path, "daily", FRI, frame)
    report = audit_partition_rows(
        tmp_path, "daily", [FRI],
        expected_fields=_DAILY_FIELDS, family_kind="daily",
    )
    # 600000.SH: null close; 600001.SH: high < open; 000001.SZ: low > high
    # and negative volume
    assert report["null_or_nonfinite_required_fields"] == 1
    assert report["ohlc_relation_violations"] == 2
    assert report["negative_volume_or_amount"] == 1
    assert report["all_rows_clean"] is False


def test_nonfinite_numeric_daily_value_is_flagged(tmp_path: Path) -> None:
    frame = pd.DataFrame({
        "instrument_id": ["600000.SH"],
        "trade_date": [FRI],
        "open": [10.0],
        "high": [11.0],
        "low": [9.0],
        "close": [10.5],
        "pre_close": [10.0],
        "volume": [float("inf")],
        "amount": [1.0e4],
    })
    _write_partition(tmp_path, "daily", FRI, frame)
    report = audit_partition_rows(
        tmp_path, "daily", [FRI],
        expected_fields=_DAILY_FIELDS, family_kind="daily",
    )
    assert report["null_or_nonfinite_required_fields"] == 1


def test_partition_date_mismatch_is_flagged(tmp_path: Path) -> None:
    frame = _base_suspension_frame()
    frame["trade_date"] = THU
    _write_partition(tmp_path, "suspensions", FRI, frame)
    report = audit_partition_rows(
        tmp_path, "suspensions", [FRI],
        expected_fields=_SUSPENSION_FIELDS, family_kind="suspensions",
    )
    assert report["date_mismatches"] == 3


@pytest.mark.parametrize("family_kind", ["daily", "stock_st", "suspensions"])
def test_families_report_their_own_key_policy(family_kind: str) -> None:
    from quantlab.execution.readiness import partition_audit_spec

    spec = partition_audit_spec(family_kind)
    assert spec.primary_key
    assert spec.required_fields
    if family_kind == "suspensions":
        assert "suspend_timing" not in spec.required_fields
        assert set(spec.primary_key) == {
            "instrument_id", "trade_date", "source_record_id",
        }
