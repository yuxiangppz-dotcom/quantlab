import json
from dataclasses import asdict
from datetime import date

import pytest

from quantlab.data.context_backfill import _hash_file, _write_json
from quantlab.data.limit_response_replay import replay_limit_responses
from quantlab.data.models import (
    DailyBar,
    DailyPriceLimit,
    DataValidationError,
    Security,
    TradingCalendar,
    canonical_payload_fingerprint,
)
from quantlab.data.storage import ParquetStorage

DAY = date(2020, 1, 2)


@pytest.fixture
def setup(tmp_path):
    storage = ParquetStorage(tmp_path / "canonical")
    storage.save_securities(
        [Security("000001.SZ", "000001", "样本", "SZSE", "SZ", "主板", "L", date(1991, 1, 1), None)]
    )
    storage.save_trading_calendar(
        [TradingCalendar("SSE", DAY, True), TradingCalendar("SZSE", DAY, True)]
    )
    storage.save_daily_bars_by_date(
        [DailyBar("000001.SZ", DAY, 10, 10, 10, 10, 10, 100, 1000)], DAY
    )
    history = tmp_path / "history.csv"
    history.write_text(
        "old_instrument_id,new_instrument_id,effective_date,old_name,original_list_date\n"
    )
    return tmp_path, storage, history


def _source(setup, kind="preflight", *, down=9):
    root, storage, history = setup
    row = DailyPriceLimit("000001.SZ", DAY, 10, 11, down, "SZSE", "tushare.stk_limit", "synthetic")
    rows = [asdict(row)]
    receipt = {
        "date": str(DAY),
        "requested_at": "2026-09-10T00:00:00+00:00",
        "observed_at": "2026-09-10T00:00:01+00:00",
        "normalized_records_sha256": canonical_payload_fingerprint({"rows": rows}),
        "daily_sha256": _hash_file(storage.daily_bars_path(DAY)),
    }
    raw = root / f"{kind}-response.json"
    source = {
        "source_manifest": {
            str(p): _hash_file(p)
            for p in (
                storage.calendar_path,
                storage.securities_path,
                history,
                storage.daily_bars_path(DAY),
            )
        }
    }
    if kind == "preflight":
        raw.write_text(json.dumps(rows, default=str))
        source.update(
            canonical_writes=False,
            results=[
                {
                    **receipt,
                    "normalized_response_path": str(raw),
                    "normalized_response_sha256": _hash_file(raw),
                }
            ],
        )
    else:
        _write_json(raw, {"request": receipt, "normalized_records": rows})
        source.update(
            schema="quantlab_historical_limit_backfill_v1",
            rejected=[{"date": str(DAY), "path": str(raw), "sha256": _hash_file(raw)}],
        )
    source["fingerprint"] = canonical_payload_fingerprint(source)
    path = root / f"{kind}-report.json"
    _write_json(path, source)
    return path


def _replay(setup, reports):
    root, storage, history = setup
    return replay_limit_responses(reports, storage, history, root / "replay", code_head="b" * 40)


@pytest.mark.parametrize("kind", ["preflight", "rejected"])
def test_offline_replay_preserves_observation_and_uses_strict_sync(setup, kind):
    source = _source(setup, kind)
    path, report = _replay(setup, [source])
    assert report["status"] == "complete" and report["provider_calls"] == 0
    assert len(report["accepted"]) == 1
    observation = json.loads((path.parent / f"observation-{DAY}.json").read_text())
    assert observation["observed_at"] == "2026-09-10T00:00:01+00:00"
    assert observation["provider_called"] is False
    _, second = _replay(setup, [source])
    assert second["accepted"] == [] and second["reused_existing"] == [str(DAY)]


def test_invalid_saved_values_are_not_made_valid(setup):
    path, report = _replay(setup, [_source(setup, "rejected", down=0)])
    assert report["status"] == "complete_with_unresolved_dates"
    assert len(report["rejected"]) == 1 and report["accepted"] == []
    assert not setup[1].daily_price_limit_exists(DAY)


def test_report_and_response_tampering_rejected_before_writes(setup):
    source = _source(setup)
    raw = setup[0] / "preflight-response.json"
    raw.write_text("[]")
    with pytest.raises(DataValidationError, match="hash"):
        _replay(setup, [source])
    assert not setup[1].daily_price_limit_exists(DAY)


def test_changed_original_inputs_reject_replay(setup):
    source = _source(setup)
    setup[2].write_text(setup[2].read_text() + "\n")
    with pytest.raises(DataValidationError, match="fixed inputs"):
        _replay(setup, [source])


def test_first_source_per_date_wins_without_silent_version_mix(setup):
    first = _source(setup, "rejected", down=0)
    second = _source(setup, "preflight", down=9)
    _, report = _replay(setup, [first, second])
    assert report["rejected"] and report["accepted"] == []
