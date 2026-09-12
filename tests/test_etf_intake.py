"""Raw ETF profiles preserve unknowns, endpoint scope and durable request accounting."""

import hashlib
import json

import pytest

from quantlab.data.etf_intake import CAPS, CODES, FIELDS, UNITS, profile, requests_for
from quantlab.data.models import DataValidationError
from quantlab.data.program_intake import Journal


def request(api):
    return next(r for r in requests_for() if r["parameters"]["api_name"] == api)


def raw(api, overrides=None, copies=1, **flags):
    names = FIELDS[api].split(",")
    row = {name: 1.0 for name in names}
    row["ts_code"] = CODES[0]
    if api == "fund_div":
        row.update({n: None for n in names if "date" in n})
        row.update(ann_date="20260102", div_proc="实施", base_year="2025")
    else:
        row["trade_date"] = "20200102"
    row.update(overrides or {})
    return json.dumps(
        {"code": 0, "data": {"fields": names, "items": [[row[n] for n in names]] * copies}, **flags}
    ).encode()


def test_exact_scope_has_no_date_filter_on_dividend_and_no_paid_endpoint():
    wanted = requests_for()
    assert len(wanted) == 20 and len({r["id"] for r in wanted}) == 20
    assert [r["parameters"]["params"]["ts_code"] for r in wanted[:5]] == list(CODES)
    for r in wanted:
        api = r["parameters"]["api_name"]
        params = r["parameters"]["params"]
        assert r["row_cap"] == CAPS[api]
        if api == "fund_div":
            assert set(params) == {"ts_code"}
        else:
            assert params["start_date"] == "20191001" and params["end_date"] == "20241231"


@pytest.mark.parametrize("api", list(FIELDS))
def test_raw_units_are_explicit_and_duplicates_are_not_dropped(api):
    result = profile(raw(api, copies=2), request(api))
    assert result["status"] == "nonempty" and result["rows"] == 2
    assert result["duplicate_key_rows"] == 1 and result["exact_duplicate_rows"] == 1
    assert result["raw_units"] == UNITS[api]
    assert result["complete_history"] is False


def test_later_dividend_and_missing_dates_are_retained_without_eligibility():
    result = profile(raw("fund_div", {"div_cash": None}), request("fund_div"))
    assert result["status"] == "nonempty"
    assert result["date_range"] == ["20260102", "20260102"]
    assert result["null_counts"]["div_cash"] == 1
    assert result["invalid_numeric_counts"]["div_cash"] == 1
    assert result["null_counts"]["ex_date"] == 1
    assert result["complete_history"] is False


@pytest.mark.parametrize(
    "override",
    [
        {"trade_date": "20260102"},
        {"trade_date": "20200230"},
        {"trade_date": None},
        {"ts_code": "159915.OF"},
    ],
)
def test_wrong_instrument_or_daily_date_is_not_silently_repaired(override):
    assert profile(raw("fund_daily", override), request("fund_daily"))["status"] == "schema_error"


def test_null_and_boolean_numbers_do_not_become_valid_prices_or_shares():
    result = profile(
        raw("fund_daily", {"open": True, "close": None, "vol": 0, "amount": -1}),
        request("fund_daily"),
    )
    assert (
        result["invalid_numeric_counts"]["open"] == result["invalid_numeric_counts"]["close"] == 1
    )
    assert (
        result["nonpositive_numeric_counts"]["vol"]
        == result["nonpositive_numeric_counts"]["amount"]
        == 1
    )
    assert result["status"] == "nonempty"


@pytest.mark.parametrize("flag", [{"has_more": True}, {"truncated": True}, {"total": 100}])
def test_truncation_stops_before_full_history_can_be_claimed(flag):
    assert profile(raw("fund_adj", **flag), request("fund_adj"))["status"] == "saturated"


def test_internal_dividend_cap_is_conservative():
    wanted = request("fund_div")
    wanted["row_cap"] = 1
    assert profile(raw("fund_div"), wanted)["status"] == "saturated"


def test_empty_distribution_does_not_mean_no_dividend():
    result = profile(
        json.dumps(
            {"code": 0, "data": {"fields": FIELDS["fund_div"].split(","), "items": []}}
        ).encode(),
        request("fund_div"),
    )
    assert result["status"] == "empty" and result["complete_history"] is False


@pytest.mark.parametrize(
    "body", [b'{"code":0,"code":0}', b'{"code":true}', b'{"code":0,"data":null}']
)
def test_bad_envelope_stays_schema_error(body):
    assert profile(body, request("fund_daily"))["status"] == "schema_error"


def test_permission_failure_has_no_provider_message_or_token_echo():
    result = profile(
        json.dumps({"code": 40203, "msg": "token secret_value invalid"}).encode(),
        request("fund_daily"),
    )
    assert result["status"] == "permission_error" and "secret_value" not in str(result)


def test_shared_journal_uses_etf_profile_and_does_not_repeat_uncertain_call(tmp_path):
    config = {"per_response_bytes": 4096, "max_body_bytes": 8192, "max_requests": 2}
    wanted = [request("fund_adj"), request("fund_share")]
    journal = Journal(tmp_path, config, wanted, "ETF", inspector=profile)
    path, intent = journal.begin(wanted[0])
    body = raw("fund_adj")
    transport = {
        "transport_status": "received",
        "http_status": 200,
        "received_bytes": len(body),
        "wire_sha256": hashlib.sha256(body).hexdigest(),
        "raw_redacted": False,
        "body_count_complete": True,
        "budget_body_bytes": len(body),
    }
    result = journal.finish(path, intent, body, transport)
    assert result["raw_units"] == UNITS["fund_adj"] and result["execution_authority"] is False
    journal.begin(wanted[1])
    reloaded = Journal(tmp_path, config, wanted, "ETF", inspector=profile)
    assert reloaded.used_bytes == len(body) + 4096
    with pytest.raises(DataValidationError, match="must not be retried"):
        reloaded.next_request()
