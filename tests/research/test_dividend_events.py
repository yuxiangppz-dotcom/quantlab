import hashlib
import json

import pytest

from quantlab.data.dividend_raw import FIELDS
from quantlab.data.models import DataValidationError
from quantlab.research.dividend_event_run import (
    OBS_SCHEMA,
    WINDOW_SCHEMA,
    saved_windows,
    write_rows,
)
from quantlab.research.dividend_events import link_window, number, observations, parse_date


def row(**updates):
    value = dict.fromkeys(FIELDS)
    value.update(
        ts_code="000002.SZ",
        end_date="20231231",
        ann_date="20240301",
        imp_ann_date="20240528",
        div_proc="实施",
        record_date="20240603",
        ex_date="20240604",
        pay_date="20240612",
        div_listdate="20240613",
        cash_div_tax=0.3,
        cash_div=0.3,
        stk_div=0.2,
        stk_bo_rate=0.1,
        stk_co_rate=0.1,
        base_date="20240520",
        base_share=1234.5,
    )
    return {**value, **updates}


def adapt(rows=None, stamp="2026-09-11T16:10:00+00:00", receipt_id="receipt"):
    rows = rows or [row()]
    fields = list(rows[0])
    raw = json.dumps(
        {"code": 0, "data": {"fields": fields, "items": [[r[k] for k in fields] for r in rows]}}
    ).encode()
    receipt = {
        "status": "nonempty",
        "observed_at": stamp,
        "rows": len(rows),
        "fingerprint": receipt_id,
        "intent_fingerprint": "intent",
        "wire_sha256": hashlib.sha256(raw).hexdigest(),
    }
    return observations(raw, receipt, "000002.SZ")


def window(**updates):
    return {
        "model": "ridge",
        "instrument_id": "000002.SZ",
        "trade_date": "2024-05-31",
        "entry_date": "2024-06-03",
        "exit_date": "2024-06-10",
        "horizon": 5,
        "entry_beyond_cutoff": False,
        "exit_beyond_cutoff": False,
        "window_source_row": 12,
        **updates,
    }


def flags(item):
    return set(json.loads(item["quality_flags"]))


def test_original_payload_units_and_occurrence_identity_survive():
    original = row(extra_provider_field="kept")
    first, duplicate = adapt([original, original])
    assert json.loads(first["raw_payload"]) == original
    assert first["cash_div_tax"] == 0.3 and first["base_share"] == 1234.5
    assert first["observation_id"] != duplicate["observation_id"]
    assert first["content_id"] == duplicate["content_id"]
    assert first["duplicate_occurrences"] == 2 and not first["candidate_conflict"]
    assert adapt([original], receipt_id="later")[0]["observation_id"] != first["observation_id"]


def test_conflicting_versions_and_duplicates_are_not_selected_or_merged():
    first, second, duplicate = adapt([row(), row(cash_div_tax=0.5), row()])
    assert first["candidate_group_id"] == second["candidate_group_id"]
    assert all(r["candidate_conflict"] for r in (first, second, duplicate))
    assert [r["cash_div_tax"] for r in (first, second, duplicate)] == [0.3, 0.5, 0.3]


@pytest.mark.parametrize("value", [True, False, -1, float("inf"), float("nan"), "0.1", 10**400])
def test_bad_numeric_values_remain_unknown(value):
    assert number(value) == (None, True)


@pytest.mark.parametrize("value", [None, ""])
def test_absent_values_are_not_zero(value):
    assert number(value) == (None, False)
    assert parse_date(value) == (None, False)


@pytest.mark.parametrize("value", ["20240230", "202461", "２０２４０６０３", 20240603])
def test_dates_are_strict_without_imputation(value):
    assert parse_date(value) == (None, True)


def test_status_normalization_is_explicit_and_proposals_never_become_implementation():
    a, b, c = adapt([row(div_proc="实施 "), row(div_proc="预案"), row(div_proc="未知新状态")])
    assert a["raw_status"] == "实施 " and a["normalized_status"] == "实施"
    assert "status_normalized" in flags(a) and a["implementation_candidate"]
    assert not b["implementation_candidate"] and "unknown_status" in flags(c)
    assert not a["cashflow_eligible"] and not a["historical_pit_certified"]


def test_conditional_date_and_component_gaps_are_separate():
    item = adapt([row(pay_date=None, div_listdate=None, stk_bo_rate=None, cash_div=None)])[0]
    assert {
        "positive_cash_pay_date_unknown",
        "positive_shares_listing_date_unknown",
        "share_components_unknown",
    } <= flags(item)
    assert item["cash_div"] is None and item["stk_bo_rate"] is None
    assert "share_components_disagree" in flags(adapt([row(stk_div=0.5)])[0])


def test_invalid_cash_is_not_treated_as_a_positive_entitlement():
    item = adapt([row(cash_div_tax=True, pay_date=None)])[0]
    assert item["cash_div_tax"] is None
    assert "malformed_cash_div_tax" in flags(item)
    assert "positive_cash_pay_date_unknown" not in flags(item)


def test_absent_status_preserves_original_payload_and_unknown_classification():
    value = adapt([row(div_proc=None)])[0]
    assert value["normalized_status"] is None and value["raw_status"] is None
    assert json.loads(value["raw_payload"])["div_proc"] is None
    assert not value["implementation_candidate"] and "unknown_status" in flags(value)


def test_chronology_errors_do_not_change_original_dates():
    item = adapt([row(ex_date="20240601", pay_date="20240601", div_listdate="20240601")])[0]
    assert {"record_after_ex", "pay_before_record", "listing_before_record"} <= flags(item)
    assert item["pay_date"] == "2024-06-01"


def test_inclusive_record_boundary_and_post_exit_events_are_candidates_only():
    rows = adapt([row(), row(record_date="20240611", ex_date="20240612")])
    summary, links = link_window(window(), rows, "2026-09-10")
    assert len(links) == 1 and links[0]["record_date_in_window"]
    assert summary["payments_after_intended_exit"] == summary["listings_after_intended_exit"] == 1
    assert summary["observed_candidates_at_signal_close"] == 0
    assert summary["entitlement_amount"] is None and not summary["execution_authority"]
    assert (
        link_window(window(entry_date="2024-06-10", exit_date="2024-06-13"), rows, "2026-09-10")[0][
            "candidate_observations"
        ]
        == 2
    )


def test_no_valid_dates_are_preserved_as_unresolved_for_the_code():
    rows = adapt([row(**dict.fromkeys(("record_date", "ex_date", "pay_date", "div_listdate")))])
    summary, links = link_window(window(), rows, "2026-09-10")
    assert not links and summary["undated_observations_for_code"] == 1
    assert summary["unknown_event_history"] is True


def test_unknown_exit_uses_fixed_cutoff_without_inventing_exit_or_cash():
    summary, links = link_window(
        window(exit_date=None, exit_beyond_cutoff=True), adapt(), "2024-06-04"
    )
    assert len(links) == 1 and summary["exit_date"] is None
    assert summary["payments_after_intended_exit"] is None
    assert links[0]["payment_after_intended_exit"] is None
    assert summary["post_exit_comparison_known"] is False
    assert summary["complete_cost_fen"] is None
    no_entry = window(
        entry_date=None, exit_date=None, entry_beyond_cutoff=True, exit_beyond_cutoff=True
    )
    assert link_window(no_entry, adapt(), "2024-06-04")[0]["candidate_observations"] is None


@pytest.mark.parametrize(
    "updates",
    [
        {"entry_date": None},
        {"exit_date": None},
        {"exit_date": "2024-06-01"},
        {"instrument_id": "600000.SH"},
    ],
)
def test_window_identity_and_cutoff_inconsistency_fail_closed(updates):
    with pytest.raises(DataValidationError):
        link_window(window(**updates), adapt(), "2026-09-10")


def test_exact_signal_close_and_shanghai_midnight_do_not_backdate_observation():
    early = adapt(stamp="2024-05-31T06:59:59+00:00")
    late = adapt(stamp="2024-05-31T07:00:01+00:00")
    assert link_window(window(), early, "2026-09-10")[0]["observed_candidates_at_signal_close"] == 1
    assert link_window(window(), late, "2026-09-10")[0]["observed_candidates_at_signal_close"] == 0
    midnight = adapt(stamp="2024-05-30T16:00:01+00:00")
    assert midnight[0]["observed_at"].startswith("2024-05-30T16:")
    future_notice = adapt([row(ann_date="20240601")], stamp="2024-05-31T06:00:00+00:00")
    assert (
        link_window(window(), future_notice, "2026-09-10")[0]["observed_candidates_at_signal_close"]
        == 0
    )
    with pytest.raises(DataValidationError):
        adapt(stamp="2024-05-31T07:00:00")


def test_changed_wire_bytes_are_rejected_before_mapping():
    with pytest.raises(DataValidationError, match="bytes changed"):
        observations(b"changed", {"wire_sha256": "0" * 64}, "000002.SZ")


def test_parquet_schema_preserves_nulls_and_refuses_overwrite(tmp_path):
    import pyarrow.parquet as pq

    rows = adapt([row(cash_div_tax=None)])
    path = tmp_path / "rows.parquet"
    write_rows(path, rows, OBS_SCHEMA)
    reread = pq.read_table(path).to_pylist()[0]
    assert reread["cash_div_tax"] is None and reread["raw_payload"] == rows[0]["raw_payload"]
    with pytest.raises(DataValidationError):
        write_rows(path, rows, OBS_SCHEMA)
    summary, _ = link_window(window(), rows, "2026-09-10")
    write_rows(tmp_path / "windows.parquet", [summary], WINDOW_SCHEMA)
    assert pq.read_table(tmp_path / "windows.parquet").to_pylist()[0]["entitlement_amount"] is None


def test_saved_window_duplicates_are_rejected(tmp_path):
    from datetime import datetime

    import pyarrow as pa
    import pyarrow.parquet as pq

    item = window()
    for key in ("trade_date", "entry_date", "exit_date"):
        item[key] = datetime.fromisoformat(item[key])
    path = tmp_path / "duplicates.parquet"
    pq.write_table(pa.Table.from_pylist([item, item]), path)
    with pytest.raises(DataValidationError, match="duplicated"):
        saved_windows(path)


@pytest.mark.parametrize(
    "change", [None, "source", "output", "authority", "population", "plan", "provider"]
)
def test_report_reader_checks_sources_outputs_counts_and_authority(tmp_path, monkeypatch, change):
    from quantlab.research import dividend_event_run as module
    from quantlab.research.input_audit import _sha
    from quantlab.research.round2_dataset import sealed_write

    # Git history verification is covered separately; isolate data and report boundaries here.
    monkeypatch.setattr(module, "verify_historical_inputs", lambda *args: None)
    root = tmp_path
    config = root / module.CONFIG
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps({"instrument_count": 1, "observation_count": 2, "window_count": 1})
    )
    (root / "source.bin").write_bytes(b"source")
    out = root / module.OUTPUT
    out.mkdir(parents=True)
    (out / "output.bin").write_bytes(b"output")
    plan = sealed_write(
        out / "plan.json",
        {
            "code_head": "a" * 40,
            "code_files": {},
            "config_sha256": _sha(config),
            "source_inputs": {"source.bin": {"sha256": _sha(root / "source.bin")}},
        },
    )
    report = {
        "plan_fingerprint": plan["fingerprint"],
        "config_sha256": _sha(config),
        "instrument_count": 1,
        "totals": {"observations_written": 2, "windows_written": 1},
        "window_summary": [{"windows": 1}],
        "new_fit_attempts": 0,
        "provider_calls": 0,
        "artifacts": {"output.bin": {"sha256": _sha(out / "output.bin")}},
        **dict.fromkeys(
            (
                "cashflow_eligible",
                "historical_pit_certified",
                "complete_event_history_certified",
                "performance_evidence",
                "execution_authority",
            ),
            False,
        ),
    }
    if change == "source":
        (root / "source.bin").write_bytes(b"different")
    elif change == "output":
        (out / "output.bin").write_bytes(b"different")
    elif change == "authority":
        report["execution_authority"] = 0
    elif change == "population":
        report["totals"]["windows_written"] = 0
    elif change == "plan":
        report["plan_fingerprint"] = "different"
    elif change == "provider":
        report["provider_calls"] = 1
    sealed_write(out / "report.json", report)
    if change is None:
        assert module.read_report(root)["totals"]["observations_written"] == 2
    else:
        with pytest.raises(DataValidationError):
            module.read_report(root)
