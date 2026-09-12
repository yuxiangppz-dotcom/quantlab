"""Fixed gap scope and raw-observation semantics using entirely synthetic bodies."""

import json

import pytest

from quantlab.data.dividend_gap_intake import (
    CODES,
    CONFIG,
    OUTPUT,
    RESOURCES,
    load_contract,
    profile,
    requests_for,
    validate_population,
)
from quantlab.data.dividend_raw import FIELDS
from quantlab.data.models import DataValidationError


def body(rows, **metadata):
    items = [[row.get(name) for name in FIELDS] for row in rows]
    return json.dumps(
        {
            "code": 0,
            "msg": "private-provider-message",
            "data": {
                "fields": list(FIELDS),
                "items": items,
                **metadata,
            },
        }
    ).encode()


def row(**changes):
    return {
        "ts_code": CODES[0],
        "end_date": "20201231",
        "div_proc": "实施",
        "cash_div_tax": 0.1,
        "record_date": "20210506",
        "ex_date": "20210507",
        **changes,
    }


def test_exact_gap_requests_use_all_fields_no_date_filter_or_duplicate():
    requests = requests_for()
    assert len(requests) == len({x["id"] for x in requests}) == 31
    assert tuple(x["parameters"]["params"]["ts_code"] for x in requests) == CODES
    assert CODES == tuple(sorted(set(CODES)))
    for request in requests:
        assert request["row_cap"] == 2000
        assert request["parameters"]["api_name"] == "dividend"
        assert set(request["parameters"]["params"]) == {"ts_code"}
        assert request["parameters"]["fields"].split(",") == list(FIELDS)


def test_new_relevance_window_does_not_relabel_legacy_2023_2026_counts():
    raw = body(
        [
            row(),
            row(record_date="20250506", ex_date="20250507"),
            row(record_date=None, ex_date=None),
        ]
    )
    result = profile(raw, requests_for()[0])
    assert result["status"] == "nonempty" and result["rows"] == 3
    stats = result["profile"]
    assert stats["rows_with_event_date_2020_2024"] == 1
    assert "rows_with_date_in_required_window" not in stats
    assert stats["relevance_window"] == ["2020-01-01", "2024-12-31"]
    assert stats["rows_without_valid_event_date"] == 1
    assert "server_message" not in result
    assert result["cashflow_eligible"] is False


def test_duplicates_nulls_and_unpaid_cash_remain_observations():
    raw = body([row(), row()])
    result = profile(raw, requests_for()[0])
    assert result["rows"] == 2
    assert result["profile"]["exact_duplicate_rows"] == 1
    assert result["profile"]["null_counts"]["pay_date"] == 2
    assert result["profile"]["implemented_positive_cash_without_valid_pay_date"] == 2


@pytest.mark.parametrize(
    "changes",
    [
        {"record_date": "20210230"},
        {"cash_div_tax": True},
        {"cash_div_tax": -0.1},
        {"cash_div_tax": "0.1"},
        {"cash_div_tax": float("nan")},
        {"base_share": False},
    ],
)
def test_malformed_nonnull_fields_stop_before_another_request(changes):
    result = profile(body([row(**changes)]), requests_for()[0])
    assert result["status"] == "schema_error"


def test_wrong_code_and_missing_field_are_schema_errors():
    assert profile(body([row(ts_code=CODES[1])]), requests_for()[0])["status"] == "schema_error"
    raw = json.dumps({"code": 0, "data": {"fields": ["ts_code"], "items": [[CODES[0]]]}}).encode()
    assert profile(raw, requests_for()[0])["status"] == "schema_error"


def test_permission_message_not_copied_to_structured_report():
    result = profile(b'{"code":2002,"msg":"permission private-marker"}', requests_for()[0])
    assert result == {"status": "permission_error", "server_code": 2002}
    assert "private-marker" not in json.dumps(result)


def test_empty_is_not_certified_no_event_history():
    result = profile(body([]), requests_for()[0])
    assert result["status"] == "empty" and result["rows"] == 0
    assert result["cashflow_eligible"] is False


@pytest.mark.parametrize("metadata", [{"has_more": True}, {"total": 2}, {"truncated": True}])
def test_explicit_truncation_stops(metadata):
    assert profile(body([row()], **metadata), requests_for()[0])["status"] == "saturated"


def test_row_saturation_stops():
    request = {**requests_for()[0], "row_cap": 1}
    assert profile(body([row()]), request)["status"] == "saturated"


def population():
    existing = [f"{i:06d}.SZ" for i in range(1, 1000) if f"{i:06d}.SZ" not in CODES][:225]
    selection = {"instrument_ids": sorted([*existing, *CODES])}
    parent = {"fingerprint": "parent", "per_code": [{"instrument_id": x} for x in existing]}
    coverage = {"report_fingerprint": "parent", "missing_codes": list(CODES)}
    return selection, parent, coverage


def test_population_is_exact_old_cohort_difference():
    selection, parent, coverage = population()
    validate_population(selection, parent, coverage)
    parent["per_code"].append({"instrument_id": CODES[0]})
    with pytest.raises(DataValidationError, match="no refetch"):
        validate_population(selection, parent, coverage)


def test_population_cannot_be_reselected_or_disconnected_from_parent():
    selection, parent, coverage = population()
    coverage["report_fingerprint"] = "different"
    with pytest.raises(DataValidationError, match="gap changed"):
        validate_population(selection, parent, coverage)
    selection["instrument_ids"][-1] = selection["instrument_ids"][0]
    with pytest.raises(DataValidationError):
        validate_population(selection, parent, coverage)


def test_contract_cannot_expand_request_budget(tmp_path):
    config = {
        "schema": "dividend_gap_intake_v1",
        "output": OUTPUT,
        "endpoint": "https://api.tushare.pro",
        "codes": list(CODES),
        "max_requests": 32,
        "max_retries": 0,
        "requests_per_minute": 100,
        "per_response_bytes": 2 * 1024**2,
        "max_body_bytes": 64 * 1024**2,
        "resources": RESOURCES,
        "economic_paths_authorized": 0,
        "model_fits_authorized": 0,
        "canonical_writes_authorized": False,
        "execution_authority": False,
        "historical_pit_certified": False,
    }
    path = tmp_path / CONFIG
    path.parent.mkdir()
    path.write_text(json.dumps(config))
    with pytest.raises(DataValidationError, match="unreviewed"):
        load_contract(tmp_path)
