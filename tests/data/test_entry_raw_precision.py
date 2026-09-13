import json
import runpy
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pytest

from quantlab.data.entry_raw_precision import (
    FIELDS,
    decoded_rows,
    load_contract,
    profile,
    raw_units,
    reconcile_row,
    requests_for,
)
from quantlab.data.models import DataValidationError

KEY = {
    "instrument_id": "002006.SZ",
    "trade_date": "2022-01-04",
    "purpose": "execution_volume_precision",
}
REQUEST = requests_for([KEY])[0]


def body(row=None, **extra):
    return json.dumps(
        {
            "code": 0,
            "data": {
                "fields": list(FIELDS),
                "items": row
                if row is not None
                else [["002006.SZ", "20220104", 1.2, 1.4, 1.1, 1.3, 227801.23, 45237.812]],
                **extra,
            },
        }
    ).encode()


def test_exact_units_and_decimal_parser():
    row = decoded_rows(body())[0]
    assert type(row["vol"]) is Decimal
    assert raw_units(row["vol"], 100) == (22780123, None)
    assert raw_units(row["amount"], 100000) == (4523781200, None)
    assert raw_units(row["open"], 100, positive=True) == (120, None)
    assert raw_units(Decimal("0"), 100) == (0, None)


@pytest.mark.parametrize(
    "value,reason",
    [
        (None, "missing"),
        (True, "not_exact_raw_number"),
        ("1", "not_exact_raw_number"),
        (1.0, "not_exact_raw_number"),
        (Decimal("NaN"), "nonfinite"),
        (Decimal("Infinity"), "nonfinite"),
        (Decimal("-1"), "invalid_sign"),
        (Decimal("1.001"), "off_integer_grid"),
        (10**15, "out_of_range"),
        (Decimal("1e999999"), "out_of_range"),
        (Decimal("1e-999999"), "out_of_range"),
    ],
)
def test_unknown_numeric_inputs_not_rounded(value, reason):
    assert raw_units(value, 100) == (None, reason)


def test_zero_price_invalid_and_bad_scale_rejected():
    assert raw_units(0, 100, positive=True) == (None, "invalid_sign")
    with pytest.raises(DataValidationError):
        raw_units(Decimal(1), 1000)


def test_exact_request_and_duplicates():
    assert REQUEST["parameters"] == {
        "api_name": "daily",
        "params": {"ts_code": "002006.SZ", "start_date": "20220104", "end_date": "20220104"},
        "fields": ",".join(FIELDS),
    }
    with pytest.raises(DataValidationError):
        requests_for([KEY, KEY])


def test_nonempty_empty_and_offgrid_observations():
    assert profile(body(), REQUEST)["status"] == "nonempty"
    assert profile(body([]), REQUEST)["status"] == "empty"
    rows = json.loads(body())["data"]["items"]
    rows[0][-2] = 1.001
    p = profile(body(rows), REQUEST)
    assert p["status"] == "nonempty" and p["unknown_units"] == {"vol": "off_integer_grid"}


@pytest.mark.parametrize(
    "mutation",
    [
        "wrong_code",
        "wrong_date",
        "duplicate",
        "bool",
        "string",
        "infinity",
        "nan",
        "bad_fields",
        "duplicate_object",
    ],
)
def test_invalid_wire_data(mutation):
    obj = json.loads(body())
    expected = "schema_error"
    if mutation == "wrong_code":
        obj["data"]["items"][0][0] = "000777.SZ"
    elif mutation == "wrong_date":
        obj["data"]["items"][0][1] = "20220105"
    elif mutation == "duplicate":
        obj["data"]["items"] *= 2
        expected = "duplicate_keys"
    elif mutation == "bad_fields":
        obj["data"]["fields"][0] = "trade_date"
    elif mutation == "duplicate_object":
        assert profile(b'{"code":0,"code":0,"data":{}}', REQUEST)["status"] == "schema_error"
        return
    else:
        obj["data"]["items"][0][-1] = {
            "bool": True,
            "string": "12",
            "infinity": float("inf"),
            "nan": float("nan"),
        }[mutation]
    assert profile(json.dumps(obj).encode(), REQUEST)["status"] == expected


@pytest.mark.parametrize("extra", [{"has_more": True}, {"truncated": True}, {"total": 2}])
def test_saturated_source_stops(extra):
    assert profile(body(**extra), REQUEST)["status"] == "saturated"


@pytest.mark.parametrize("total", [False, -1, 0, 1.0])
def test_invalid_total(total):
    assert profile(body(total=total), REQUEST)["status"] == "schema_error"


@pytest.mark.parametrize(
    "code,msg,status",
    [
        (2002, "secret", "permission_error"),
        (1, "token bad", "permission_error"),
        (1, "server bad", "provider_error"),
    ],
)
def test_provider_errors_sanitized(code, msg, status):
    r = profile(json.dumps({"code": code, "msg": msg}).encode(), REQUEST)
    assert r["status"] == status and "msg" not in r


def test_reconciliation_preserves_old_values_and_unknowns():
    raw = decoded_rows(body())[0]
    old = {k: float(raw[k]) for k in ("open", "high", "low", "close")}
    old.update(volume=float(raw["vol"]) * 100, amount=float(raw["amount"]) * 1000)
    before = deepcopy(old)
    r = reconcile_row(KEY, raw, old)
    assert old == before and r["raw_normalized"]["vol"] == 22780123
    assert r["raw_ohlc_consistent"] is True
    assert all(x["float_normalization_reproduces_saved"] for x in r["comparisons"].values())
    assert r["automatically_admitted"] is False and r["historical_pit_certified"] is False
    empty = reconcile_row(KEY, None, None)
    assert all(v is None for v in empty["raw_normalized"].values())
    assert empty["raw_ohlc_consistent"] is None and empty["canonical_row_present"] is False


def test_conflicting_old_data_not_silently_repaired():
    raw = decoded_rows(body())[0]
    r = reconcile_row(KEY, raw, {"volume": 13})
    assert r["comparisons"]["vol"]["exact_value_equal"] is False
    assert r["comparisons"]["vol"]["float_normalization_reproduces_saved"] is False
    assert r["comparisons"]["vol"]["saved_canonical_text"] == "13"
    raw["low"] = Decimal("2")
    assert reconcile_row(KEY, raw, None)["raw_ohlc_consistent"] is False


def test_manifest_mutation_rejected_before_reads(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/s4_entry_raw_precision_v1.json").write_text("{}")
    with pytest.raises(DataValidationError, match="contract changed"):
        load_contract(tmp_path)


def test_independent_digit_exponent_arithmetic():
    proof = runpy.run_path(
        str(Path(__file__).parents[2] / "scripts/prove_s4_entry_raw_precision.py")
    )
    convert = proof["scalar_units"]
    assert convert(Decimal("227801.23"), 100, False) == (22780123, None)
    assert convert(Decimal("45237.812"), 100000, False) == (4523781200, None)
    assert convert(Decimal("1.001"), 100, False) == (None, "off_integer_grid")
    assert convert(Decimal("0.00"), 100, True) == (None, "invalid_sign")
