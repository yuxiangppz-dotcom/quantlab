import copy
import json
from datetime import date
from pathlib import Path

import pytest

from quantlab.data.models import DataValidationError
from quantlab.execution.rules import default_a_share_rule_book
from quantlab.research.input_audit import _sha
from quantlab.research.rule_evidence import EvidenceCatalogue, audit_dates
from quantlab.research.rule_evidence_v2 import (
    CONFIG,
    MANIFEST,
    PARENT_REPORT,
    compare_to_v1,
    validate_extension,
)

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def inputs():
    parent = EvidenceCatalogue(
        json.loads((ROOT / "config/historical_rule_catalogue_v1.json").read_text()),
        json.loads((ROOT / "config/historical_rule_sources_v1.json").read_text()),
    )
    return (json.loads((ROOT / CONFIG).read_text()),
            json.loads((ROOT / MANIFEST).read_text()), parent)


@pytest.mark.parametrize("exchange,board", [
    ("SSE", "MAIN"), ("SSE", "STAR"), ("SZSE", "MAIN"), ("SZSE", "CHINEXT"),
])
def test_early_gap_and_exact_transition_preserve_existing_rules(inputs, exchange, board):
    c, m, parent = inputs
    new = validate_extension(c, m, parent)
    assert new.resolve(exchange, board, date(2022, 12, 30)) is None
    for day in (date(2023, 1, 1), date(2023, 2, 17), date(2023, 4, 9)):
        assert parent.resolve(exchange, board, day) is None
        assert new.resolve(exchange, board, day).rule_id.endswith("pre2023-evidence-v2")
    for day in (date(2023, 4, 10), date(2026, 7, 5), date(2026, 7, 6), date(2026, 9, 10)):
        assert new.resolve(exchange, board, day).rule_id == parent.resolve(
            exchange, board, day,
        ).rule_id
    assert new.resolve(exchange, board, date(2026, 9, 11)) is None
    assert new.resolve(exchange, board, date(2023, 1, 3), fields=["price_cage"]) is None


@pytest.mark.parametrize("bad", [
    "old_value", "old_source", "missing_scope", "widened_gap", "missing_chain",
    "future_publication", "late_effectiveness", "wrong_composite_date", "unbound_source",
    "calendar", "missing_scope_review", "no_amendment", "wrong_supersession", "old_document",
])
def test_extension_cannot_rewrite_frozen_evidence_or_guess_effectiveness(inputs, bad):
    c, m, parent = inputs
    r = c["intervals"][-1]
    if bad == "old_value":
        c["intervals"][0]["values"]["price_tick"] = "0.001"
    elif bad == "old_source":
        c["intervals"][0]["source_ids"].pop()
    elif bad == "missing_scope":
        c["intervals"].pop()
    elif bad == "widened_gap":
        r["from"] = "2022-12-31"
    elif bad == "missing_chain":
        r["source_chain"] = []
    elif bad == "future_publication":
        m["documents"]["szse_2022_block_notice"]["published_on"] = "2022-08-23"
    elif bad == "late_effectiveness":
        r["source_chain"][0]["effective_from"] = "2023-01-02"
    elif bad == "wrong_composite_date":
        r["legal_effective_from"] = "2022-08-19"
    elif bad == "unbound_source":
        r["source_chain"][0]["source_id"] = "sse_2020_rules"
    elif bad == "calendar":
        c["calendar"]["expected_sessions"] = 894
    elif bad == "missing_scope_review":
        r["source_chain"][0]["scope_note"] = ""
    elif bad == "no_amendment":
        r["source_chain"] = [e for e in r["source_chain"]
                             if e["kind"] != "amendment_outside_order_scope"]
    elif bad == "wrong_supersession":
        r["superseded_on"] = "2023-02-17"
    elif bad == "old_document":
        m["documents"]["sse_2020_rules"]["published_on"] = "2020-03-12"
    with pytest.raises(DataValidationError):
        validate_extension(c, m, parent)


def test_small_date_population_reconciles_v1_to_v2_without_changing_baseline(inputs):
    c, m, old = inputs
    new = validate_extension(c, m, old)
    days = [date(2023, 1, 3), date(2023, 4, 7), date(2023, 4, 10)]
    baseline = default_a_share_rule_book()
    parent = {**audit_dates(old, days, baseline), "fingerprint": PARENT_REPORT}
    current = audit_dates(new, days, baseline)
    changes = compare_to_v1(current, parent)
    assert changes["newly_covered_rows"] == 8
    assert changes["unchanged_rows"] == 4
    assert changes["still_unknown_rows"] == 0
    assert all(not r["differences"] for r in current["rows"])
    assert baseline.resolve("SZSE", "MAIN", date(2021, 3, 31)).version == "2021.03"
    broken = copy.deepcopy(current)
    broken["rows"][0]["session"] = "2023-01-04"
    with pytest.raises(DataValidationError, match="populations"):
        compare_to_v1(broken, parent)
    broken = copy.deepcopy(current)
    broken["rows"][2]["catalogue_values"]["price_tick"] = "0.001"
    with pytest.raises(DataValidationError, match="previously covered"):
        compare_to_v1(broken, parent)


def test_original_contract_and_resolver_bytes_stay_bound(inputs):
    _, manifest, _ = inputs
    for name in ("config/historical_rule_catalogue_v1.json",
                 "config/historical_rule_sources_v1.json",
                 "src/quantlab/research/rule_evidence.py", "scripts/audit_historical_rules.py",
                 "src/quantlab/execution/rules.py", "src/quantlab/personal/quantity_rules.py"):
        assert _sha(ROOT / name) == manifest["files"][name]["sha256"]
    assert _sha(ROOT / CONFIG) == manifest["contract_sha256"]


def test_primary_effective_dates_are_not_publication_dates(inputs):
    config, _, _ = inputs
    early = {r["exchange"]: r for r in config["intervals"] if "source_chain" in r}
    assert early["SSE"]["legal_effective_from"] == "2022-09-05"
    assert early["SZSE"]["legal_effective_from"] == "2022-08-22"
    szse = {e["source_id"]: e["effective_from"] for e in early["SZSE"]["source_chain"]}
    assert szse["szse_2021_rules"] == "2021-04-06"
    assert szse["szse_chinext_2020_notice"] == "2020-08-24"
