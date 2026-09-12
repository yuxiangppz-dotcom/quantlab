import copy
import json
from dataclasses import asdict
from datetime import date
from pathlib import Path

import pytest

from quantlab.data.models import DataValidationError
from quantlab.execution.rules import OrderSession, OrderType, default_a_share_rule_book
from quantlab.research.input_audit import _sha
from quantlab.research.rule_evidence import EvidenceCatalogue, audit_dates
from quantlab.research.rule_evidence_v3 import (
    CONFIG,
    MANIFEST,
    PARENT_REPORT,
    EarlierCatalogue,
    compare_to_v2,
)

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def inputs():
    parent = EvidenceCatalogue(
        json.loads((ROOT / "config/historical_rule_catalogue_v2.json").read_text()),
        json.loads((ROOT / "config/historical_rule_sources_v2.json").read_text()),
    )
    return json.loads((ROOT / CONFIG).read_text()), parent


@pytest.mark.parametrize(
    "exchange,board,start,before,amend",
    [
        ("SSE", "MAIN", "2020-03-13", "2022-09-04", "2022-09-05"),
        ("SSE", "STAR", "2020-03-13", "2022-09-04", "2022-09-05"),
        ("SZSE", "MAIN", "2021-04-06", "2022-08-21", "2022-08-22"),
        ("SZSE", "CHINEXT", "2021-04-06", "2022-08-21", "2022-08-22"),
    ],
)
def test_earlier_boundaries_and_original_payloads(inputs, exchange, board, start, before, amend):
    c, parent = inputs
    old_contract = copy.deepcopy(parent.contract)
    new = EarlierCatalogue(c, parent)
    for text in (start, before, amend, "2022-12-31"):
        day = date.fromisoformat(text)
        assert parent.resolve(exchange, board, day) is None
        result = new.resolve(exchange, board, day)
        assert result is not None
        assert result.sellability_lag_sessions == 1
        assert result.buy_min_quantity == (200 if board == "STAR" else 100)
        assert result.buy_quantity_step == (1 if board == "STAR" else 100)
        assert result.max_limit_quantity == (
            100000 if board == "STAR" else 300000 if board == "CHINEXT" else 1000000
        )
    base = new.resolve(exchange, board, date.fromisoformat(before))
    after = new.resolve(exchange, board, date.fromisoformat(amend))
    assert base.rule_id != after.rule_id
    record = new.records[base.rule_id]
    assert not any("2022_block" in s for s in record["source_ids"])
    assert any("2022_block" in s for s in record["supersession_evidence"]["source_ids"])
    for text in ("2023-01-01", "2023-04-10", "2026-07-06", "2026-09-10"):
        day = date.fromisoformat(text)
        assert asdict(new.resolve(exchange, board, day)) == asdict(
            parent.resolve(exchange, board, day)
        )
    assert new.resolve(exchange, board, date(2026, 9, 11)) is None
    assert parent.contract == old_contract


@pytest.mark.parametrize(
    "exchange,board,day",
    [
        ("SSE", "MAIN", date(2020, 3, 12)),
        ("SSE", "STAR", date(2020, 1, 2)),
        ("SZSE", "MAIN", date(2021, 3, 31)),
        ("SZSE", "CHINEXT", date(2021, 4, 5)),
    ],
)
def test_missing_older_rules_are_not_extrapolated(inputs, exchange, board, day):
    c, parent = inputs
    assert EarlierCatalogue(c, parent).resolve(exchange, board, day) is None


@pytest.mark.parametrize(
    "bad",
    [
        "old_value",
        "old_identity",
        "start",
        "end",
        "source_date",
        "count",
        "duplicate",
        "legal_date",
        "supersession",
        "calendar",
        "authority",
        "audit_scope",
        "extra_field",
    ],
)
def test_finite_extension_cannot_rewrite_scope_or_history(inputs, bad):
    c, parent = inputs
    if bad == "old_value":
        parent.contract["intervals"][0]["values"]["price_tick"] = "0.1"
    elif bad == "old_identity":
        parent.contract["intervals"][0]["id"] = "replaced"
    elif bad == "start":
        c["additions"][0]["from"] = "2020-01-01"
    elif bad == "end":
        c["additions"][0]["through"] = "2023-01-01"
    elif bad == "source_date":
        parent.manifest["documents"]["sse_2020_notice"]["published_on"] = "2020-03-14"
    elif bad == "count":
        c["additions"].pop()
    elif bad == "duplicate":
        c["additions"][-1] = c["additions"][0]
    elif bad == "legal_date":
        c["additions"][4]["legal_effective_from"] = "2021-03-31"
    elif bad == "supersession":
        c["additions"][0]["superseded_on"] = "2023-04-10"
    elif bad == "calendar":
        c["calendar"]["expected_sessions"] -= 1
    elif bad == "authority":
        c["execution_authority"] = True
    elif bad == "audit_scope":
        c["audit_interval"][0] = "2019-01-01"
    elif bad == "extra_field":
        c["additions"][0]["price_cage"] = "assumed"
    with pytest.raises(DataValidationError):
        EarlierCatalogue(c, parent)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"order_type": OrderType.MARKET},
        {"session": OrderSession.CLOSING_AUCTION},
        {"fields": ["price_cage"]},
    ],
)
def test_subset_does_not_become_closing_auction_or_execution_authority(inputs, kwargs):
    c, parent = inputs
    assert EarlierCatalogue(c, parent).resolve("SSE", "MAIN", date(2022, 1, 4), **kwargs) is None


def test_saved_parent_rows_are_not_recomputed_or_silently_changed(inputs):
    c, parent = inputs
    new = EarlierCatalogue(c, parent)
    baseline = default_a_share_rule_book()
    old = {**audit_dates(parent, [date(2023, 1, 3)], baseline), "fingerprint": PARENT_REPORT}
    current = audit_dates(new, [date(2022, 1, 4), date(2023, 1, 3)], baseline)
    proof = compare_to_v2(current, old, new)
    assert proof["unchanged_rows"] == proof["unchanged_resolver_payloads"] == 4
    assert proof["covered_2022"] == 4
    current["rows"][-1]["catalogue_values"]["price_tick"] = "0.001"
    with pytest.raises(DataValidationError):
        compare_to_v2(current, old, new)


def test_parent_code_and_contracts_stay_byte_bound():
    m = json.loads((ROOT / MANIFEST).read_text())
    assert _sha(ROOT / CONFIG) == m["contract_sha256"]
    for name, entry in m["files"].items():
        if name.startswith(("config/", "src/")):
            assert _sha(ROOT / name) == entry["sha256"]
