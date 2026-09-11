import subprocess
import sys
from datetime import date

import numpy as np
import pandas as pd
import pytest

from quantlab.data.models import AdjFactor, DailyBar, DataValidationError, Security, TradingCalendar
from quantlab.data.storage import ParquetStorage
from quantlab.research.alpha158_history_checks import compare_overlap
from quantlab.research.alpha158_history_data import (
    discover_inventory,
    ingest_month,
    map_history_inputs,
)
from quantlab.research.alpha158_native import FIELDS
from quantlab.research.alpha158_staging import checked_plan, partition_folder, verify_sources
from quantlab.research.alpha158_store import (
    Budget,
    Partitions,
    atomic_seal,
    directory_bytes,
    exclusive_job,
)
from quantlab.research.round2_dataset import sealed_read


def budget(out):
    return Budget(
        out,
        {
            "max_generated_bytes": 10**8,
            "reserve_host_D_bytes": 0,
            "max_rss_bytes": 10 * 1024**3,
            "max_wakeup_seconds": 10,
            "next_partition_time_reserve_seconds": 1,
        },
        disk_root=out,
    )


def test_atomic_receipt_and_exclusive_worker_never_overwrite(tmp_path):
    path = tmp_path / "receipt.json"
    atomic_seal(path, {"value": 1})
    with pytest.raises(FileExistsError):
        atomic_seal(path, {"value": 2})
    assert sealed_read(path)["value"] == 1
    with exclusive_job(tmp_path):
        with pytest.raises(DataValidationError, match="another"):
            with exclusive_job(tmp_path):
                pass
    with exclusive_job(tmp_path):
        pass


def test_complete_partition_resumes_without_recomputation_and_detects_tampering(tmp_path):
    store = Partitions(tmp_path, "fixed", budget(tmp_path))

    def action(folder):
        (folder / "data.bin").write_bytes(b"synthetic")
        return {"count": 1}

    first = store.execute("a", 10000, 0, action)
    assert store.execute("a", 10000, 0, lambda _: pytest.fail("recomputed")) == first
    (tmp_path / "attempts/a/0001/data.bin").write_bytes(b"modified")
    with pytest.raises(DataValidationError, match="changed"):
        store.read("a")
    with pytest.raises(DataValidationError, match="unsafe"):
        store.read("../bad")


def test_abandoned_attempt_is_preserved_and_budget_is_not_reset(tmp_path):
    folder = tmp_path / "attempts/a/0001"
    folder.mkdir(parents=True)
    atomic_seal(folder / "started.json", {"identity": "fixed", "name": "a"})
    (folder / "partial.bin").write_bytes(b"partial")
    before = directory_bytes(tmp_path)
    store = Partitions(tmp_path, "fixed", budget(tmp_path))
    result = store.execute("a", 10000, 0, lambda _: {"count": 1})
    assert result["attempt"] == 2 and (folder / "partial.bin").read_bytes() == b"partial"
    assert directory_bytes(tmp_path) > before
    other = Partitions(tmp_path, "changed", budget(tmp_path))
    with pytest.raises(DataValidationError, match="identity"):
        other.read("a")


def test_terminal_failures_malformed_attempts_and_changed_plans_block(tmp_path):
    store = Partitions(tmp_path, "fixed", budget(tmp_path))

    def fail(_):
        raise ValueError("synthetic deterministic failure")

    with pytest.raises(ValueError):
        store.execute("failed", 10000, 0, fail)
    with pytest.raises(DataValidationError, match="terminal"):
        store.execute("failed", 10000, 0, lambda _: {})
    partial = tmp_path / "attempts/malformed/0001"
    partial.mkdir(parents=True)
    (partial / "started.json").write_text("{}")
    with pytest.raises(DataValidationError, match="fingerprint"):
        store.execute("malformed", 10000, 0, lambda _: {})
    checked_plan(tmp_path, {"code": "same", "inputs": "v1"})
    with pytest.raises(DataValidationError, match="resume"):
        checked_plan(tmp_path, {"code": "changed", "inputs": "v1"})


def test_budget_checks_next_write_and_checkpoint_time(tmp_path, monkeypatch):
    guard = budget(tmp_path)
    guard.config["max_generated_bytes"] = 100
    (tmp_path / "partial").write_bytes(b"x" * 80)
    with pytest.raises(DataValidationError, match="disk budget"):
        guard.check(21)
    guard.config["max_rss_bytes"] = 1
    with pytest.raises(DataValidationError, match="RSS"):
        guard.check(0)
    guard.config["max_rss_bytes"] = 10 * 1024**3
    guard.started -= 11
    store = Partitions(tmp_path, "fixed", guard)
    assert store.execute("later", 0, 0, lambda _: pytest.fail("ran")) is None
    assert not (tmp_path / "attempts/later").exists()


def test_resource_watchdog_stops_only_its_subprocess_and_preserves_evidence(tmp_path):
    script = """from pathlib import Path
import time
from quantlab.research.alpha158_store import Budget
out=Path(__import__('sys').argv[1])
b=Budget(out, {'max_rss_bytes':1,'max_wakeup_seconds':100})
with b.watchdog(): time.sleep(3)
"""
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path)], timeout=15)
    assert result.returncode == 73
    interruptions = list((tmp_path / "interruptions").glob("*.json"))
    assert len(interruptions) == 1
    assert sealed_read(interruptions[0])["reason"] == "resource_watchdog"


def synthetic_storage(tmp_path):
    storage = ParquetStorage(tmp_path / "data/canonical")
    storage.save_securities(
        [
            Security(
                "000001.SZ",
                "000001",
                "SYNTHETIC",
                "SZSE",
                "SZ",
                "主板",
                "D",
                date(2000, 1, 1),
                date(2020, 1, 8),
            )
        ]
    )
    days = pd.date_range("2020-01-01", "2020-01-10")
    storage.save_trading_calendar(
        [TradingCalendar(e, d.date(), d.weekday() < 5) for e in ("SSE", "SZSE") for d in days]
    )
    for d in days:
        if d.weekday() >= 5 or str(d.date()) == "2020-01-07":
            continue
        codes = ["000001.SZ", "600000.SH", "200001.SZ"]
        storage.save_daily_bars_by_date(
            [DailyBar(c, d.date(), 10.0, 11.0, 9.0, 10.0, 10.0, 100.0, 1000.0) for c in codes],
            d.date(),
        )
        storage.save_adj_factors_by_date([AdjFactor("000001.SZ", d.date(), 2.0)], d.date())
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "security_code_changes.csv").write_text(
        "old_instrument_id,new_instrument_id,effective_date,old_name,original_list_date\n"
    )
    return {"start": "2020-01-02", "end": "2020-01-10", "warmup_sessions": 1, "batch_codes": 32}


def test_history_universe_raw_staging_missing_dates_and_unknown_lifecycle(tmp_path):
    config = synthetic_storage(tmp_path)
    inventory = discover_inventory(tmp_path, config)
    assert inventory["instruments"] == ["000001.SZ", "600000.SH"]
    assert inventory["outside_v1_observed_codes"] == ["200001.SZ"]
    assert inventory["list_dates"]["600000.SH"] is None
    assert not inventory["historical_market_coverage_complete"]
    folder = tmp_path / "isolated"
    folder.mkdir()
    receipt = ingest_month(tmp_path, folder, "2020-01", inventory, config)
    raw = pd.read_parquet(folder / "batch-0000.parquet")
    assert len(raw) == receipt["raw_rows"]
    mapped, evidence = map_history_inputs(
        raw.assign(future_return_5d=999), inventory["sessions"], inventory["instruments"], inventory
    )
    assert len(mapped) == 16
    unknown = evidence.instrument_id.eq("600000.SH")
    assert evidence.loc[unknown, "lifecycle_active"].isna().all()
    assert mapped.loc[unknown, FIELDS].isna().all().all()
    assert evidence.loc[unknown, "exclusion_reason"].eq("unknown_lifecycle").all()
    gap = evidence.instrument_id.eq("000001.SZ") & evidence.trade_date.eq("2020-01-07")
    assert evidence.loc[gap, "exclusion_reason"].eq("missing_daily").all()
    dead = evidence.instrument_id.eq("000001.SZ") & evidence.trade_date.gt("2020-01-08")
    assert mapped.loc[dead, FIELDS].isna().all().all()
    verify_sources(tmp_path, inventory)
    with pytest.raises(DataValidationError, match="escaped"):
        map_history_inputs(
            raw.assign(trade_date=pd.Timestamp("2099-01-01")),
            inventory["sessions"],
            inventory["instruments"],
            inventory,
        )
    source = tmp_path / inventory["sources"][0]["path"]
    source.write_bytes(b"changed")
    with pytest.raises(DataValidationError, match="changed"):
        verify_sources(tmp_path, inventory)


def test_overlap_requires_exact_membership_missingness_and_predeclared_tolerance(tmp_path):
    frame = pd.DataFrame(
        {
            "instrument_id": ["000001.SZ"] * 2,
            "trade_date": pd.bdate_range("2020-01-01", periods=2),
            "MA5": [1.0, np.nan],
        }
    )
    config = {"overlap_rtol": 1e-5, "overlap_atol": 1e-6}
    assert compare_overlap(frame, frame.copy(), ["MA5"], config)["mismatches"] == 0
    with pytest.raises(DataValidationError, match="missingness"):
        compare_overlap(frame, frame.fillna({"MA5": 0}), ["MA5"], config)
    with pytest.raises(DataValidationError, match="numerical"):
        compare_overlap(frame, frame.assign(MA5=[2.0, np.nan]), ["MA5"], config)
    with pytest.raises(DataValidationError, match="identity"):
        compare_overlap(frame.iloc[:1], frame, ["MA5"], config)
    with pytest.raises(DataValidationError, match="escaped"):
        partition_folder(tmp_path, tmp_path / "inside", {"result": {"folder": "outside"}})
