import hashlib
import io
import json
import time
from contextlib import nullcontext

import pytest

from quantlab.data.dividend_acquisition import Journal
from quantlab.data.dividend_raw import FIELDS, WireClient, inspect_response
from quantlab.data.models import DataValidationError, canonical_payload_fingerprint
from quantlab.research.round2_dataset import sealed_read


def payload(code="000001.SZ", **changes):
    row = dict.fromkeys(FIELDS)
    row.update(
        ts_code=code,
        end_date="20221231",
        ann_date="20230301",
        div_proc="实施",
        stk_div=0,
        stk_bo_rate=0,
        stk_co_rate=0,
        cash_div=0.18,
        cash_div_tax=0.2,
        record_date="20230602",
        ex_date="20230605",
        pay_date="20230605",
    )
    row.update(changes)
    return json.dumps(
        {
            "code": 0,
            "msg": None,
            "data": {
                "fields": list(FIELDS),
                "items": [[row[key] for key in FIELDS]],
            },
        }
    ).encode()


def transport(raw, status="received"):
    return {
        "received_bytes": len(raw),
        "transport_status": status,
        "wire_sha256": hashlib.sha256(raw).hexdigest(),
        "raw_redacted": False,
    }


@pytest.fixture
def journal(tmp_path):
    return Journal(
        tmp_path,
        {
            "fields": list(FIELDS),
            "row_cap": 2000,
            "per_response_bytes": 4096,
            "max_retries": 1,
            "max_body_bytes": 20000,
        },
        ["000001.SZ", "600000.SH"],
        "fixed",
    )


def finish(journal, status="received"):
    code = journal.next_code()
    path, intent = journal.begin(code)
    raw = payload(code)
    return journal.finish(path, intent, raw, transport(raw, status))


def reload(journal):
    return Journal(journal.out, journal.config, journal.codes, journal.identity)


def test_provider_fields_are_preserved_without_tax_or_share_conversion():
    r = inspect_response(payload(base_share=1234.5), "000001.SZ")
    assert r["status"] == "nonempty"
    assert r["profile"]["null_counts"]["base_share"] == 0
    assert r["profile"]["rows_with_date_in_required_window"] == 1
    assert r["profile"]["implemented_positive_cash_without_valid_pay_date"] == 0


def test_missing_dates_proposals_and_unknowns_are_retained():
    raw = payload(
        div_proc="预案",
        record_date=None,
        ex_date=None,
        pay_date=None,
        cash_div=None,
        cash_div_tax=None,
        stk_div=None,
    )
    r = inspect_response(raw, "000001.SZ")
    assert r["status"] == "nonempty"
    assert r["profile"]["rows_without_valid_event_date"] == 1
    assert r["profile"]["null_counts"]["cash_div_tax"] == 1
    assert r["profile"]["status_counts"] == {"预案": 1}


def test_bad_dates_and_units_are_flagged_not_repaired():
    r = inspect_response(payload(pay_date="20230230", base_share=-10), "000001.SZ")
    assert r["profile"]["malformed_counts"]["pay_date"] == 1
    assert r["profile"]["malformed_counts"]["base_share"] == 1
    assert r["profile"]["implemented_positive_cash_without_valid_pay_date"] == 1


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b'{"code":0,"code":1}',
        b'{"code":true}',
        b'{"code":0,"data":null}',
        payload("600000.SH"),
    ],
)
def test_malformed_or_cross_instrument_response_fails_closed(raw):
    assert inspect_response(raw, "000001.SZ")["status"] == "schema_error"


@pytest.mark.parametrize("mutation", ["missing_field", "duplicate_field", "short_row", "nan"])
def test_schema_shape_must_match(mutation):
    p = json.loads(payload())
    if mutation == "missing_field":
        p["data"]["fields"] = list(FIELDS[:-1])
        p["data"]["items"][0] = p["data"]["items"][0][:-1]
    elif mutation == "duplicate_field":
        p["data"]["fields"][1] = p["data"]["fields"][0]
    elif mutation == "short_row":
        p["data"]["items"][0].pop()
    else:
        p["data"]["items"][0][-1] = float("nan")
    assert inspect_response(json.dumps(p), "000001.SZ")["status"] == "schema_error"


def test_empty_and_saturated_are_distinct_from_no_action():
    p = json.loads(payload())
    p["data"]["items"] = []
    assert inspect_response(json.dumps(p), "000001.SZ")["status"] == "empty"
    assert inspect_response(payload(), "000001.SZ", row_cap=1)["status"] == "saturated"
    p = json.loads(payload())
    p["data"]["total"] = 5
    assert inspect_response(json.dumps(p), "000001.SZ")["status"] == "saturated"


def test_duplicates_and_conflicting_candidates_are_not_merged():
    p = json.loads(payload())
    p["data"]["items"] *= 2
    other = list(p["data"]["items"][0])
    other[FIELDS.index("cash_div_tax")] = 0.5
    p["data"]["items"].append(other)
    r = inspect_response(json.dumps(p), "000001.SZ")
    assert r["rows"] == 3
    assert r["profile"]["exact_duplicate_rows"] == 1
    assert r["profile"]["candidate_identity_conflicts"] == 1


@pytest.mark.parametrize(
    "code,msg,expected",
    [
        (2002, "denied", "permission_error"),
        (-1, "token invalid", "permission_error"),
        (-1, "每分钟限次", "rate_limited"),
        (-1, "other failure", "provider_error"),
    ],
)
def test_error_classification(code, msg, expected):
    raw = json.dumps({"code": code, "msg": msg, "data": None})
    assert inspect_response(raw, "000001.SZ")["status"] == expected


def test_success_receipt_is_not_refetched_on_resume(journal):
    finish(journal)
    resumed = reload(journal)
    assert resumed.next_code() == "600000.SH"
    finish(resumed)
    assert reload(resumed).next_code() is None
    assert resumed.summary()["attempts"] == 2
    assert resumed.summary()["complete_event_history_certified"] is False


def test_interrupted_request_counts_reserved_bytes_and_is_not_replayed(journal):
    journal.begin("000001.SZ")
    resumed = reload(journal)
    assert resumed.used_bytes == 4096
    assert resumed.summary()["status_counts"] == {"interrupted_unknown": 1, "pending": 1}
    finish(resumed)
    assert reload(resumed).next_code() is None


def test_retries_only_after_initial_scope_and_once_with_global_cap(journal):
    finish(journal, "transport_error")
    assert journal.next_code() == "600000.SH"
    finish(journal, "transport_error")
    assert journal.next_code() == "000001.SZ"
    finish(journal, "transport_error")
    assert journal.next_code() is None
    resumed = reload(journal)
    assert resumed.retries == 1 and resumed.attempts == 3


@pytest.mark.parametrize("status", ["permission_error", "schema_error", "body_limit"])
def test_nontransient_results_never_retry(journal, status):
    finish(journal, status)
    finish(journal)
    assert reload(journal).next_code() is None


def test_budget_reserves_full_unknown_response_before_network(journal):
    journal.config["max_body_bytes"] = 4095
    with pytest.raises(DataValidationError, match="body budget"):
        journal.begin("000001.SZ")
    assert not (journal.out / "attempts").exists()


def test_request_order_and_body_tampering_are_rejected(journal):
    with pytest.raises(DataValidationError, match="order"):
        journal.begin("600000.SH")
    finish(journal)
    (journal.out / "attempts/000001.SZ/01/response.body").write_bytes(b"changed")
    with pytest.raises(DataValidationError, match="bound file"):
        reload(journal)


@pytest.mark.parametrize("field,value", [("execution_authority", True), ("wire_sha256", "wrong")])
def test_resealed_result_cannot_upgrade_authority_or_change_wire_evidence(journal, field, value):
    finish(journal)
    path = journal.out / "attempts/000001.SZ/01/result.json"
    result = json.loads(path.read_text())
    result.pop("fingerprint")
    result[field] = value
    result["fingerprint"] = canonical_payload_fingerprint(result)
    path.write_text(json.dumps(result))
    with pytest.raises(DataValidationError):
        reload(journal)


class FakeResponse:
    def __init__(self, body, status=200):
        self.raw, self.status_code = io.BytesIO(body), status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class FakeSession:
    def __init__(self, response):
        self.response, self.calls = response, []

    def post(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.response


def test_wire_does_one_request_no_redirect_or_token_in_receipt():
    session = FakeSession(FakeResponse(b'{"msg":"test-secret"}', 302))
    client = WireClient("test-secret", "https://api.tushare.pro", session=session)
    raw, r = client.fetch({"api_name": "dividend"}, 4096)
    assert len(session.calls) == 1
    assert session.calls[0][1]["allow_redirects"] is False
    assert session.calls[0][1]["json"]["token"] == "test-secret"
    assert b"test-secret" not in raw and "test-secret" not in json.dumps(r)
    assert r["transport_status"] == "secret_echo" and r["raw_redacted"] is True


def test_wire_caps_partial_body_and_does_not_parse_as_complete():
    session = FakeSession(FakeResponse(b"x" * 50))
    raw, r = WireClient("test-secret", "https://api.tushare.pro", session=session).fetch({}, 20)
    assert len(raw) == r["received_bytes"] == 20
    assert r["transport_status"] == "body_limit"


@pytest.fixture
def runner(tmp_path, monkeypatch):
    from quantlab.data import dividend_acquisition as module

    config = {
        "fields": list(FIELDS),
        "row_cap": 2000,
        "per_response_bytes": 4096,
        "max_retries": 1,
        "max_body_bytes": 20000,
        "requests_per_minute": 120,
        "request_fingerprint": "fixture",
        "endpoint": "https://api.tushare.pro",
        "resources": {"reserve_host_D_bytes": 0},
    }
    path = tmp_path / module.CONFIG
    path.parent.mkdir(parents=True)
    path.write_text("{}")
    monkeypatch.setenv("TUSHARE_TOKEN", "fixture-secret")
    monkeypatch.setattr(module, "load_contract", lambda root: (config, ["000001.SZ", "600000.SH"]))
    monkeypatch.setattr(
        module.subprocess, "check_output", lambda args, **kwargs: "" if "status" in args else "head"
    )
    monkeypatch.setattr(module, "code_binding", lambda *args: "head")
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)

    class FakeBudget:
        checks = 0
        checkpoint = False

        def __init__(self, out, config):
            self.started, self.disk_root = time.monotonic(), tmp_path

        def check(self, **kwargs):
            return 0

        def can_start(self):
            self.checks += 1
            return not self.checkpoint or self.checks <= 2

        def watchdog(self):
            return nullcontext()

    class Client:
        status = "received"
        calls = []
        fail = False

        def __init__(self, token, endpoint):
            pass

        def fetch(self, parameters, cap):
            self.calls.append(parameters)
            if self.fail:
                raise RuntimeError("fixture-secret must not leak")
            raw = payload(parameters["params"]["ts_code"])
            return raw, transport(raw, self.status)

        def close(self):
            pass

    monkeypatch.setattr(module, "Budget", FakeBudget)
    return module, Client, FakeBudget, tmp_path


def test_end_to_end_synthetic_run_resume_and_sealed_artifacts(runner):
    module, client, _, root = runner
    r = module.run(root, client_factory=client)
    assert r["attempts"] == 2 and r["returned_rows"] == 2
    assert r["initial_scope_attempted"] is True
    assert r["stop_reason"] == "request_scope_exhausted"
    assert r["execution_authority"] is False
    pointer = sealed_read(root / module.OUTPUT / "latest.json")
    assert pointer["report_fingerprint"] == r["fingerprint"]
    again = module.run(root, client_factory=client)
    assert again["attempts"] == 2 and len(client.calls) == 2


def test_permission_failure_stops_whole_batch_and_future_wakes(runner):
    module, client, _, root = runner
    client.status = "permission_error"
    r = module.run(root, client_factory=client)
    assert r["attempts"] == 1 and r["status_counts"]["pending"] == 1
    assert r["stop_reason"] == "permission_error"
    with pytest.raises(DataValidationError, match="prior acquisition blocker"):
        module.run(root, client_factory=client)
    assert len(client.calls) == 1


def test_wakeup_checkpoint_preserves_remaining_scope(runner):
    module, client, budget, root = runner
    budget.checkpoint = True
    r = module.run(root, client_factory=client)
    assert r["stop_reason"] == "wakeup_checkpoint"
    assert r["status_counts"] == {"nonempty": 1, "pending": 1}
    assert len(client.calls) == 1


def test_unexpected_failure_retains_intent_without_secret_traceback(runner):
    module, client, _, root = runner
    client.fail = True
    r = module.run(root, client_factory=client)
    assert r["stop_reason"] == "runtime_or_budget_error"
    assert r["status_counts"] == {"interrupted_unknown": 1, "pending": 1}
    assert r["charged_body_bytes"] == 4096
    for path in (root / module.OUTPUT).rglob("*.json"):
        assert "fixture-secret" not in path.read_text()


def test_truncated_chunked_wire_charges_consumed_but_unretained_bytes(journal):
    from http.client import HTTPResponse as NativeResponse

    from urllib3.response import HTTPResponse

    class Socket:
        def makefile(self, *args):
            return io.BytesIO(
                b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n2000\r\npartial"
            )

    native = NativeResponse(Socket())
    native.begin()
    response = FakeResponse(b"")
    response.raw = HTTPResponse(
        body=native,
        headers=dict(native.getheaders()),
        original_response=native,
        preload_content=False,
    )
    client = WireClient("test-secret", "https://api.tushare.pro", session=FakeSession(response))
    raw, result = client.fetch({}, 4096)
    assert raw == b""  # The underlying chunked reader consumed seven bytes before failing.
    assert result["transport_status"] == "transport_error"
    assert result["body_count_complete"] is False
    assert result["budget_body_bytes"] == 4096
    path, intent = journal.begin(journal.next_code())
    journal.finish(path, intent, raw, result)
    assert journal.used_bytes == reload(journal).used_bytes == 4096
    assert journal.summary()["retained_response_bytes"] == 0
    assert journal.summary()["uncertain_body_attempts"] == 1


def test_complete_wire_only_charges_exact_body_bytes(journal):
    raw = payload()
    client = WireClient(
        "test-secret", "https://api.tushare.pro", session=FakeSession(FakeResponse(raw))
    )
    body, result = client.fetch({}, 4096)
    assert result["body_count_complete"] is True
    assert result["budget_body_bytes"] == len(body) == len(raw)
    path, intent = journal.begin(journal.next_code())
    journal.finish(path, intent, body, result)
    assert journal.used_bytes == reload(journal).used_bytes == len(raw)


def test_legacy_failure_conservatively_reserves_without_rewriting_receipt(journal):
    result = finish(journal, "transport_error")
    path = journal.out / "attempts/000001.SZ/01/result.json"
    result = {
        key: value
        for key, value in result.items()
        if key not in ("fingerprint", "body_count_complete", "budget_body_bytes")
    }
    result["fingerprint"] = canonical_payload_fingerprint(result)
    raw = json.dumps(result).encode()
    path.write_bytes(raw)
    assert reload(journal).used_bytes == 4096
    assert path.read_bytes() == raw


@pytest.mark.parametrize(
    "changes",
    [
        {"budget_body_bytes": 0},
        {"body_count_complete": True},
        {"budget_body_bytes": True},
        {"body_count_complete": 1},
    ],
)
def test_failure_receipt_cannot_release_uncertain_reservation(journal, changes):
    result = finish(journal, "transport_error")
    path = journal.out / "attempts/000001.SZ/01/result.json"
    result.pop("fingerprint")
    result.update(changes)
    result["fingerprint"] = canonical_payload_fingerprint(result)
    path.write_text(json.dumps(result))
    with pytest.raises(DataValidationError):
        reload(journal)


def test_boolean_cash_cannot_be_counted_as_positive_dividend():
    result = inspect_response(payload(cash_div_tax=True, pay_date=None), "000001.SZ")
    assert result["profile"]["malformed_counts"]["cash_div_tax"] == 1
    assert result["profile"]["implemented_positive_cash_without_valid_pay_date"] == 0


def test_incomplete_transport_cannot_be_treated_as_received(journal):
    raw = payload()
    state = {**transport(raw), "body_count_complete": False, "budget_body_bytes": 4096}
    path, intent = journal.begin(journal.next_code())
    with pytest.raises(DataValidationError):
        journal.finish(path, intent, raw, state)
    assert not (path / "result.json").exists()
    assert reload(journal).used_bytes == 4096
