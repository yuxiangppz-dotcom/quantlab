"""A finite prerequisite sample preserves unknowns and consumes failed intents."""

import hashlib
import json
from collections import Counter

import pytest

from quantlab.data.context_evidence_intake import (
    CAPS,
    CODES,
    DAYS,
    FIELDS,
    TEXT,
    profile,
    requests_for,
)
from quantlab.data.models import DataValidationError
from quantlab.data.program_intake import Journal, acquire


def request(api):
    return next(r for r in requests_for() if r["parameters"]["api_name"] == api)


def raw(api, overrides=None, copies=1, **flags):
    fields = FIELDS[api].split(",")
    row = {key: None if key in TEXT else 1.0 for key in fields}
    row["ts_code"] = CODES[0]
    if api == "daily_basic":
        row["trade_date"] = DAYS[0]
    elif api == "index_member_all":
        row.update(l3_code="801010.SI", in_date="20150101", is_new="Y")
    else:
        row.update(ann_date="20210102", end_date="20201231")
    row.update(overrides or {})
    return json.dumps(
        {
            "code": 0,
            "data": {"fields": fields, "items": [[row[k] for k in fields]] * copies},
            **flags,
        }
    ).encode()


def test_exact_43_requests_have_no_vip_alias_or_unregistered_dates():
    wanted = requests_for()
    assert len(wanted) == len({r["id"] for r in wanted}) == 43
    assert Counter(r["parameters"]["api_name"] for r in wanted) == {
        "daily_basic": 11,
        "index_member_all": 16,
        "forecast": 8,
        "express": 8,
    }
    assert [r["parameters"]["params"]["trade_date"] for r in wanted[:11]] == list(DAYS)
    assert [
        (r["parameters"]["params"]["ts_code"], r["parameters"]["params"]["is_new"])
        for r in wanted[11:27]
    ] == [(c, f) for c in CODES for f in ("Y", "N")]
    for r in wanted:
        api = r["parameters"]["api_name"]
        assert r["row_cap"] == CAPS[api]
        assert r["parameters"]["fields"] == FIELDS[api]
        if api in ("forecast", "express"):
            assert r["parameters"]["params"]["start_date"] == "20200101"
            assert r["parameters"]["params"]["end_date"] == "20241231"


@pytest.mark.parametrize("api", list(FIELDS))
def test_duplicates_and_missing_values_never_certify_history(api):
    result = profile(raw(api, copies=2), request(api))
    assert result["rows"] == 2 and result["status"] == "nonempty"
    assert result["duplicate_key_rows"] == result["exact_duplicate_rows"] == 1
    for key in ("complete_history", "historical_pit_certified", "strategy_input_eligible"):
        assert result[key] is False
    empty = profile(raw(api, copies=0), request(api))
    assert empty["status"] == "empty" and empty["strategy_input_eligible"] is False


def test_unknown_valuation_is_not_zero_and_financial_losses_remain_negative():
    result = profile(raw("daily_basic", {"pe": None, "pb": -0.5}), request("daily_basic"))
    assert result["null_counts"]["pe"] == 1 and result["status"] == "nonempty"
    result = profile(raw("forecast", {"net_profit_min": -10}), request("forecast"))
    assert result["status"] == "nonempty"
    result = profile(raw("index_member_all"), request("index_member_all"))
    assert result["null_counts"]["out_date"] == 1


@pytest.mark.parametrize(
    "api,changes",
    [
        ("daily_basic", {"trade_date": "20200102"}),
        ("daily_basic", {"trade_date": "20190230"}),
        ("daily_basic", {"trade_date": None}),
        ("daily_basic", {"pe": True}),
        ("daily_basic", {"pe": float("nan")}),
        ("daily_basic", {"pb": "1.0"}),
        ("index_member_all", {"ts_code": "600000.SH"}),
        ("index_member_all", {"is_new": "N"}),
        ("index_member_all", {"in_date": "20210101", "out_date": "20200101"}),
        ("index_member_all", {"l3_name": 1}),
        ("forecast", {"ann_date": "20250101"}),
        ("forecast", {"ann_date": None}),
        ("forecast", {"first_ann_date": "20200100"}),
        ("express", {"ts_code": "600000.SH"}),
        ("express", {"revenue": float("inf")}),
        ("express", {"end_date": "20201232"}),
    ],
)
def test_bad_scope_or_schema_stops_without_silent_repair(api, changes):
    assert profile(raw(api, changes), request(api))["status"] == "schema_error"


@pytest.mark.parametrize("flags", [{"has_more": True}, {"truncated": True}, {"total": 10}])
def test_truncation_is_a_stop(flags):
    assert profile(raw("express", **flags), request("express"))["status"] == "saturated"


def test_local_express_cap_is_not_a_completeness_statement():
    wanted = request("express")
    wanted["row_cap"] = 1
    assert profile(raw("express"), wanted)["status"] == "saturated"


@pytest.mark.parametrize(
    "body",
    [
        b'{"code":true}',
        b'{"code":0,"code":0}',
        b'{"code":0,"data":null}',
    ],
)
def test_bad_envelopes_fail_closed(body):
    assert profile(body, request("daily_basic"))["status"] == "schema_error"


def test_permission_response_has_no_message_or_secret_echo():
    result = profile(b'{"code":2002,"msg":"token secret_value"}', request("express"))
    assert result == {"status": "permission_error", "server_code": 2002, "rows": None}


def transport(body):
    return {
        "transport_status": "received",
        "http_status": 200,
        "received_bytes": len(body),
        "wire_sha256": hashlib.sha256(body).hexdigest(),
        "raw_redacted": False,
        "body_count_complete": True,
        "budget_body_bytes": len(body),
    }


def test_bad_first_response_consumes_one_attempt_and_prevents_second_request(tmp_path):
    config = {"per_response_bytes": 4096, "max_body_bytes": 8192, "max_requests": 2}
    wanted = [request("forecast"), request("express")]
    journal = Journal(tmp_path, config, wanted, "context", inspector=profile)

    class Budget:
        def can_start(self):
            return True

        def check(self, **kwargs):
            pass

    class Client:
        calls = 0

        def fetch(self, params, cap):
            self.calls += 1
            body = b'{"code":2002,"msg":"permission"}'
            return body, transport(body)

    client = Client()
    assert acquire(journal, Budget(), client, gap_seconds=0) == "permission_error"
    assert client.calls == 1 and journal.summary()["requests_attempted"] == 1
    reloaded = Journal(tmp_path, config, wanted, "context", inspector=profile)
    with pytest.raises(DataValidationError, match="must not be retried"):
        reloaded.next_request()


def test_unfinished_intent_reserves_full_bytes_and_cannot_resume(tmp_path):
    config = {"per_response_bytes": 4096, "max_body_bytes": 8192, "max_requests": 2}
    wanted = [request("forecast"), request("express")]
    Journal(tmp_path, config, wanted, "context", inspector=profile).begin(wanted[0])
    journal = Journal(tmp_path, config, wanted, "context", inspector=profile)
    assert journal.used_bytes == 4096
    with pytest.raises(DataValidationError, match="must not be retried"):
        journal.next_request()
