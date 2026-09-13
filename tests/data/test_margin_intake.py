import json
from copy import deepcopy

import pytest

from quantlab.data.margin_intake import load_contract, profile, requests_for
from quantlab.data.models import DataValidationError

REQUEST = requests_for(["000004.SZ"])[0]


def body(rows=None, **extra):
    return json.dumps(
        {
            "code": 0,
            "data": {
                "fields": ["ts_code", "trade_date", "rzye"],
                "items": rows if rows is not None else [["000004.SZ", "20200102", 123.4]],
                **extra,
            },
        }
    ).encode()


def test_fixed_requests():
    r = requests_for(["000004.SZ", "600000.SH"])
    assert len(r) == 2 and r[1]["parameters"]["params"]["ts_code"] == "600000.SH"
    assert r[0]["parameters"] == {
        "api_name": "margin_detail",
        "params": {"ts_code": "000004.SZ", "start_date": "20191224", "end_date": "20241230"},
        "fields": "ts_code,trade_date,rzye",
    }
    assert r[0]["row_cap"] == 6000


@pytest.mark.parametrize(
    "codes", [[], ["bad"], ["000001.BJ"], ["600000.SH", "000004.SZ"], ["000004.SZ"] * 2]
)
def test_invalid_cohort(codes):
    with pytest.raises(DataValidationError):
        requests_for(codes)


def test_plain_empty_and_null_negative_remain_observations():
    assert profile(body(), REQUEST)["status"] == "nonempty"
    empty = profile(body([]), REQUEST)
    assert empty["status"] == "empty" and empty["date_range"] is None
    result = profile(
        body([["000004.SZ", "20200102", None], ["000004.SZ", "20200103", -1]]), REQUEST
    )
    assert result["null_rzye_rows"] == result["negative_rzye_rows"] == 1
    assert (
        not result["historical_pit_certified"] and not result["margin_target_eligibility_certified"]
    )


@pytest.mark.parametrize("value", [True, False, "123", [], float("inf"), float("nan")])
def test_invalid_measure(value):
    assert profile(body([["000004.SZ", "20200102", value]]), REQUEST)["status"] == "schema_error"


@pytest.mark.parametrize("day", ["20191223", "20241231", "20200230", "2020-01-02", None, 20200102])
def test_date_scope(day):
    assert profile(body([["000004.SZ", day, 1]]), REQUEST)["status"] == "schema_error"


def test_exact_exchange_dates_and_code():
    assert profile(body(), REQUEST, allowed_dates={"20200103"})["status"] == "schema_error"
    assert profile(body([["600000.SH", "20200102", 1]]), REQUEST)["status"] == "schema_error"


@pytest.mark.parametrize("extra", [{"total": 2}, {"has_more": True}, {"truncated": True}])
def test_truncation(extra):
    assert profile(body(**extra), REQUEST)["status"] == "saturated"


def test_cap_and_duplicates_stop():
    request = deepcopy(REQUEST)
    request["row_cap"] = 1
    assert profile(body(), request)["status"] == "saturated"
    assert profile(body([["000004.SZ", "20200102", 1]] * 2), REQUEST)["status"] == "duplicate_keys"


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"code": 2002, "msg": "secret unavailable"}, "permission_error"),
        ({"code": 1, "msg": "token invalid"}, "permission_error"),
        ({"code": 1, "msg": "server error"}, "provider_error"),
        ({"code": True}, "schema_error"),
        ({"code": 0, "data": None}, "schema_error"),
    ],
)
def test_errors_are_sanitized(payload, expected):
    result = profile(json.dumps(payload).encode(), REQUEST)
    assert result["status"] == expected and "msg" not in result


def test_bad_fields_and_total():
    payload = json.loads(body())
    payload["data"]["fields"] = ["ts_code", "ts_code", "rzye"]
    assert profile(json.dumps(payload).encode(), REQUEST)["status"] == "schema_error"
    assert profile(body(total=False), REQUEST)["status"] == "schema_error"
    assert profile(body(total=0), REQUEST)["status"] == "schema_error"


def test_frozen_contract_rejects_change_before_data_access(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/f7_margin_intake_v1.json").write_text("{}")
    with pytest.raises(DataValidationError, match="contract changed"):
        load_contract(tmp_path)
