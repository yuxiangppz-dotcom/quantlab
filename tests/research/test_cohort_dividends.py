import copy
import hashlib
import json

import pytest

from quantlab.data.dividend_raw import FIELDS
from quantlab.data.models import DataValidationError
from quantlab.data.models import canonical_payload_fingerprint as fingerprint
from quantlab.research.cohort_dividends import adapt_response, classify, summarize, utc_stamp


def raw_row(**updates):
    row = dict.fromkeys(FIELDS)
    row.update(
        ts_code="000001.SZ",
        end_date="20191231",
        ann_date="20200301",
        div_proc="实施",
        stk_div=0,
        stk_bo_rate=0,
        stk_co_rate=0,
        cash_div=0.08,
        cash_div_tax=0.1,
        record_date="20200601",
        ex_date="20200602",
        pay_date="20200602",
        imp_ann_date="20200525",
    )
    return {**row, **updates}


def parse(rows=None, batch="legacy", receipt_updates=None, intent_updates=None):
    rows = rows if rows is not None else [raw_row()]
    raw = json.dumps(
        {"code": 0, "data": {"fields": FIELDS, "items": [[r[k] for k in FIELDS] for r in rows]}}
    ).encode()
    parameters = {
        "api_name": "dividend",
        "params": {"ts_code": "000001.SZ"},
        "fields": ",".join(FIELDS),
    }
    intent = (
        {"parameters": parameters}
        if batch == "legacy"
        else {"request": {"parameters": parameters}, "at": "2026-09-12T19:00:00+00:00"}
    )
    intent.update(intent_updates or {})
    intent["fingerprint"] = fingerprint(intent)
    receipt = {
        "wire_sha256": hashlib.sha256(raw).hexdigest(),
        "status": "nonempty",
        "rows": len(rows),
        "intent_fingerprint": intent["fingerprint"],
    }
    if batch == "legacy":
        receipt["observed_at"] = "2026-09-11T16:00:00+00:00"
    else:
        receipt.update(at="2026-09-12T19:00:01+00:00", raw_redacted=False, body_count_complete=True)
    receipt.update(receipt_updates or {})
    receipt["fingerprint"] = fingerprint(receipt)
    before = copy.deepcopy(receipt)
    mapped = adapt_response(raw, receipt, intent, "000001.SZ", batch)
    assert receipt == before
    return mapped


@pytest.mark.parametrize(
    "batch,source", [("legacy", "wire_observed_at"), ("gap", "receipt_recorded_at")]
)
def test_local_availability_is_provenance_labeled_and_never_historical_authority(batch, source):
    rows, collisions = classify(parse(batch=batch))
    row = rows[0]
    assert row["availability_bound_source"] == source
    assert row["historical_pit_certified"] is False
    assert row["cashflow_eligible"] is False
    assert row["structurally_ready"] is True  # base_share is not per-share structure
    assert not collisions
    if batch == "gap":
        assert row["source_observed_at"] is None
        assert row["observed_at"] == row["source_receipt_at"]


@pytest.mark.parametrize(
    "stamp", [None, "bad", "2020-01-01T12:00:00", "2020-01-01T12:00:00+08:00", 123]
)
def test_timestamp_requires_aware_utc(stamp):
    with pytest.raises(DataValidationError, match="UTC"):
        utc_stamp(stamp)


@pytest.mark.parametrize(
    "changes",
    [
        {"at": "2026-09-12T18:59:59+00:00"},
        {"at": None},
        {"raw_redacted": True},
        {"body_count_complete": False},
        {"intent_fingerprint": "wrong"},
        {"wire_sha256": "wrong"},
        {"rows": 99},
    ],
)
def test_source_failures_do_not_become_observations(changes):
    with pytest.raises(DataValidationError):
        parse(batch="gap", receipt_updates=changes)


def test_wrong_instrument_rejected():
    with pytest.raises(DataValidationError, match="another code"):
        parse([raw_row(ts_code="000002.SZ")])


def test_wrong_request_rejected():
    with pytest.raises(DataValidationError, match="parameters"):
        parse(intent_updates={"parameters": {}})


def test_all_occurrences_and_proposal_stages_survive_without_extra_payouts():
    rows, collisions = classify(parse([raw_row(), raw_row(), raw_row(div_proc="预案")]))
    assert len(rows) == 3 and len({r["observation_id"] for r in rows}) == 3
    assert rows[0]["content_id"] == rows[1]["content_id"]
    assert not rows[2]["structurally_ready"]
    assert rows[2]["possible_event_group_id"] is None
    assert not collisions
    s = summarize(rows)
    assert s["exact_duplicate_excess"] == 1
    assert s["structurally_ready_contents"] == 1
    assert s["counts"]["implementation_occurrences"] == 2


def test_changed_announcement_does_not_hide_terms_collision():
    rows, collisions = classify(parse([raw_row(), raw_row(ann_date="20200302", cash_div_tax=0.2)]))
    assert len(collisions) == 1
    assert all(r["possible_event_conflict"] for r in rows)
    assert not any(r["candidate_conflict"] for r in rows)
    assert not any(r["structurally_ready"] for r in rows)
    assert collisions[0]["resolved"] is False
    assert set(collisions[0]["observation_ids"]) == {r["observation_id"] for r in rows}


def test_same_terms_new_announcement_is_retained_not_a_conflict_or_verified_event():
    rows, collisions = classify(parse([raw_row(), raw_row(ann_date="20200302")]))
    assert len(rows) == 2 and not collisions
    assert all(r["structurally_ready"] for r in rows)
    assert all(r["cashflow_eligible"] is False for r in rows)


def test_parser_candidate_conflicts_are_still_blockers():
    rows, _ = classify(parse([raw_row(), raw_row(base_share=1)]))
    assert all(r["candidate_conflict"] and not r["structurally_ready"] for r in rows)


@pytest.mark.parametrize(
    "updates,blocker",
    [
        ({"cash_div_tax": None}, "cash_before_tax_unknown"),
        ({"cash_div_tax": -1}, "malformed_cash_div_tax"),
        ({"pay_date": None}, "positive_cash_pay_date_unknown"),
        ({"record_date": None}, "implemented_record_or_ex_unknown"),
        ({"ex_date": "20200230"}, "malformed_ex_date"),
        ({"record_date": "20200603"}, "record_after_ex"),
        ({"pay_date": "20200501"}, "pay_before_record"),
        ({"stk_div": 0.1, "stk_bo_rate": 0.1}, "positive_shares_listing_date_unknown"),
        ({"stk_div": 0.1}, "share_components_disagree"),
        ({"stk_co_rate": None}, "share_components_unknown"),
        ({"div_proc": "未知"}, "unknown_status"),
        ({"div_proc": "停止实施"}, "not_implementation"),
    ],
)
def test_specific_structure_gaps_remain_blocked(updates, blocker):
    rows, _ = classify(parse([raw_row(**updates)]))
    assert rows[0]["structurally_ready"] is False
    assert blocker in json.loads(rows[0]["structural_blockers"])


def test_zero_cash_does_not_require_payment_but_is_not_an_entitlement():
    rows, _ = classify(parse([raw_row(cash_div_tax=0, cash_div=0, pay_date=None)]))
    assert rows[0]["structurally_ready"] is True
    assert rows[0]["cashflow_eligible"] is False


def test_no_dates_is_unknown_and_outside_observations_do_not_assert_complete_history():
    rows, _ = classify(
        parse(
            [
                raw_row(record_date=None, ex_date=None, pay_date=None),
                raw_row(record_date="20191230", ex_date="20191231", pay_date=None),
                raw_row(record_date="20191230", ex_date="20191231", pay_date="20200102"),
            ]
        )
    )
    assert [r["observed_date_scope"] for r in rows] == [
        "no_valid_event_dates",
        "no_observed_date_in_window",
        "in_window",
    ]


def test_existing_authority_cannot_be_passed_through_as_true():
    rows = parse()
    rows[0]["cashflow_eligible"] = True
    with pytest.raises(DataValidationError, match="authority"):
        classify(rows)
