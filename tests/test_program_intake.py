"""Durable bounded requests must not create false historical evidence or retry I/O."""

import hashlib
import json
from unittest.mock import Mock

import pytest

from quantlab.data.models import DataValidationError
from quantlab.data.program_intake import (
    FLOW_FIELDS,
    FUND_FIELDS,
    Journal,
    acquire,
    inspect,
    requests_for,
)
from quantlab.research.alpha158_store import atomic_seal


def wire(fields, rows, **extra):
    return json.dumps(
        {"code": 0, "data": {"fields": list(fields), "items": rows}, **extra}
    ).encode()


def flow_raw(*, day="20200102", code="000001.SZ"):
    row = [code, day, *([1.0] * (len(FLOW_FIELDS) - 2))]
    return wire(FLOW_FIELDS, [row])


def transport(raw, *, complete=True, status="received", cap=4096):
    return {
        "transport_status": status,
        "http_status": 200,
        "received_bytes": len(raw),
        "wire_sha256": hashlib.sha256(raw).hexdigest(),
        "raw_redacted": False,
        "body_count_complete": complete,
        "budget_body_bytes": len(raw) if complete else cap,
    }


@pytest.fixture
def fixture(tmp_path):
    requests = requests_for(["000001.SZ"])
    config = {"per_response_bytes": 4096, "max_body_bytes": 16384, "max_requests": 4}
    return tmp_path, config, requests


def test_request_scope_is_exact_and_retains_terminated_funds():
    requests = requests_for(["000001.SZ", "600000.SH"])
    assert len(requests) == 5
    assert [r["parameters"]["params"]["status"] for r in requests[:3]] == ["D", "I", "L"]
    assert requests[-1]["parameters"]["params"] == {
        "ts_code": "600000.SH",
        "start_date": "20191001",
        "end_date": "20241231",
    }
    assert not any("token" in r["parameters"] for r in requests)


@pytest.mark.parametrize(
    "codes", [[], ["../secret"], ["600000.SH", "000001.SZ"], ["000001.SZ", "000001.SZ"]]
)
def test_request_selection_cannot_change_order_or_escape(codes):
    with pytest.raises(DataValidationError):
        requests_for(codes)


def test_request_dates_cannot_extend_observed_window():
    with pytest.raises(DataValidationError):
        requests_for(["000001.SZ"], end="20260910")


def test_duplicate_rows_and_nulls_are_retained_as_quality_evidence():
    row = ["000001.SZ", "20200102", *([None] * (len(FLOW_FIELDS) - 2))]
    profile = inspect(wire(FLOW_FIELDS, [row, row]), requests_for(["000001.SZ"])[-1])
    assert profile["status"] == "nonempty"
    assert profile["rows"] == 2 and profile["duplicate_key_rows"] == 1
    assert profile["null_counts"]["buy_lg_amount"] == 2
    assert profile["date_range"] == ["20200102", "20200102"]


@pytest.mark.parametrize(
    "day,code", [("20250201", "000001.SZ"), ("20200230", "000001.SZ"), ("20200102", "000002.SZ")]
)
def test_wrong_date_or_instrument_blocks(day, code):
    assert inspect(flow_raw(day=day, code=code), requests_for(["000001.SZ"])[-1])["status"] == (
        "schema_error"
    )


@pytest.mark.parametrize(
    "raw", [b'{"code":0,"code":0}', b'{"code":true}', b'{"code":0,"data":null}', b"not json"]
)
def test_malformed_envelopes_do_not_become_empty_success(raw):
    assert inspect(raw, requests_for(["000001.SZ"])[-1])["status"] == "schema_error"


def test_saturation_and_empty_are_distinct():
    request = requests_for(["000001.SZ"])[-1]
    assert inspect(wire(FLOW_FIELDS, []), request)["status"] == "empty"
    assert inspect(wire(FLOW_FIELDS, [], has_more=True), request)["status"] == "saturated"
    request["row_cap"] = 1
    assert inspect(flow_raw(), request)["status"] == "saturated"


def test_permission_error_stays_explicit():
    raw = json.dumps({"code": 40203, "msg": "接口无权限"}).encode()
    assert inspect(raw, requests_for(["000001.SZ"])[0])["status"] == "permission_error"


def test_missing_result_is_charged_and_not_retried(fixture):
    out, config, requests = fixture
    journal = Journal(out, config, requests, "fixed")
    journal.begin(requests[0])
    resumed = Journal(out, config, requests, "fixed")
    assert resumed.used_bytes == 4096 and resumed.summary()["requests_attempted"] == 1
    with pytest.raises(DataValidationError, match="must not be retried"):
        resumed.next_request()


def test_resume_keeps_success_and_does_not_repeat_request(fixture):
    out, config, requests = fixture
    journal = Journal(out, config, requests, "fixed")
    path, intent = journal.begin(requests[0])
    raw = wire(FUND_FIELDS, [])
    journal.finish(path, intent, raw, transport(raw))
    resumed = Journal(out, config, requests, "fixed")
    assert resumed.next_request() == requests[1]
    assert resumed.used_bytes == len(raw)


def test_raw_tamper_and_code_identity_change_are_rejected(fixture):
    out, config, requests = fixture
    journal = Journal(out, config, requests, "fixed")
    path, intent = journal.begin(requests[0])
    raw = wire(FUND_FIELDS, [])
    journal.finish(path, intent, raw, transport(raw))
    with pytest.raises(DataValidationError, match="intent changed"):
        Journal(out, config, requests, "different_source_head")
    (path / "response.body").write_bytes(b"changed")
    with pytest.raises(DataValidationError, match="bound file changed"):
        Journal(out, config, requests, "fixed")


def test_intent_tampering_is_rejected(fixture):
    out, config, requests = fixture
    journal = Journal(out, config, requests, "fixed")
    path, _ = journal.begin(requests[0])
    intent = json.loads((path / "intent.json").read_text())
    intent["request"]["parameters"]["params"]["status"] = "L"
    (path / "intent.json").write_text(json.dumps(intent))
    with pytest.raises(DataValidationError, match="fingerprint mismatch"):
        Journal(out, config, requests, "fixed")


def test_body_budget_reserves_before_io(fixture):
    out, config, requests = fixture
    config["max_body_bytes"] = 4095
    journal = Journal(out, config, requests, "fixed")
    with pytest.raises(DataValidationError, match="reservation"):
        journal.begin(requests[0])
    assert not (out / "attempts").exists()


def test_unknown_request_directory_and_gap_fail_closed(fixture):
    out, config, requests = fixture
    (out / "attempts" / "unregistered").mkdir(parents=True)
    with pytest.raises(DataValidationError, match="unregistered"):
        Journal(out, config, requests, "fixed")


def test_response_failure_stops_batch_without_retry(fixture):
    out, config, requests = fixture
    journal = Journal(out, config, requests, "fixed")
    raw = b'{"code":40203,"msg":"permission"}'
    client = Mock()
    client.fetch.return_value = raw, transport(raw)
    budget = Mock()
    budget.can_start.return_value = True
    assert acquire(journal, budget, client, gap_seconds=0) == "permission_error"
    assert client.fetch.call_count == 1
    with pytest.raises(DataValidationError, match="must not be retried"):
        Journal(out, config, requests, "fixed").next_request()


def test_completed_scope_reuses_all_attempts_without_network(fixture):
    out, config, requests = fixture
    journal = Journal(out, config, requests, "fixed")
    client = Mock()
    client.fetch.side_effect = [
        (raw, transport(raw)) for raw in [wire(FUND_FIELDS, [])] * 3 + [flow_raw()]
    ]
    budget = Mock()
    budget.can_start.return_value = True
    assert acquire(journal, budget, client, gap_seconds=0) == "request_scope_exhausted"
    assert client.fetch.call_count == 4
    resumed = Journal(out, config, requests, "fixed")
    acquire(resumed, budget, client, gap_seconds=0)
    assert client.fetch.call_count == 4
    assert resumed.summary()["economic_paths_used"] == 0
    assert resumed.summary()["historical_pit_certified"] is False


def test_checkpoint_does_not_consume_unstarted_request(fixture):
    out, config, requests = fixture
    journal = Journal(out, config, requests, "fixed")
    budget, client = Mock(), Mock()
    budget.can_start.return_value = False
    assert acquire(journal, budget, client, gap_seconds=0) == "wakeup_checkpoint"
    assert journal.summary()["requests_attempted"] == 0
    client.fetch.assert_not_called()


def test_altered_fund_status_is_not_silently_accepted():
    row = dict.fromkeys(FUND_FIELDS)
    row.update(ts_code="510300.SH", market="E", status="L")
    raw = wire(FUND_FIELDS, [list(row.values())])
    assert inspect(raw, requests_for(["000001.SZ"])[0])["status"] == "schema_error"


def test_noncontiguous_attempt_is_not_a_valid_resume(fixture):
    out, config, requests = fixture
    atomic_seal(out / "attempts" / requests[1]["id"] / "intent.json", {})
    with pytest.raises(DataValidationError, match="order changed"):
        Journal(out, config, requests, "fixed")
