import copy
import json
from datetime import date
from pathlib import Path

import pytest

from quantlab.data.models import DataValidationError
from quantlab.execution.models import ExecutionValidationError, OrderType, Side
from quantlab.execution.rules import default_a_share_rule_book
from quantlab.research.rule_evidence import (
    CONFIG,
    MANIFEST,
    SCOPES,
    EvidenceCatalogue,
    audit_dates,
    baseline_fingerprint,
    load_catalogue,
    values,
)

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def inputs():
    return (json.loads((ROOT / CONFIG).read_text()), json.loads((ROOT / MANIFEST).read_text()))


@pytest.fixture
def catalogue(inputs):
    return EvidenceCatalogue(*inputs)


@pytest.mark.parametrize("exchange,board", SCOPES)
def test_publication_is_not_effective_date_and_supersession_is_exclusive(
    catalogue, exchange, board,
):
    for day in (date(2023, 2, 17), date(2023, 4, 9)):
        assert catalogue.resolve(exchange, board, day) is None
    for day, edition in ((date(2023, 4, 10), "2023"), (date(2026, 4, 24), "2023"),
                         (date(2026, 7, 5), "2023"), (date(2026, 7, 6), "2026"),
                         (date(2026, 9, 10), "2026")):
        rule = catalogue.resolve(exchange, board, day)
        assert catalogue.records[rule.rule_id]["edition"] == edition
    assert catalogue.resolve(exchange, board, date(2026, 9, 11)) is None


def test_unknown_order_scope_and_fields_stay_unknown(catalogue):
    args = ("SSE", "MAIN", date(2025, 1, 2))
    assert catalogue.resolve(*args, order_type=OrderType.MARKET) is None
    assert catalogue.resolve(*args, session="closing_auction") is None
    for field in ("tradable", "price_limit", "fees", "board_identity", "fill_price"):
        assert catalogue.resolve(*args, fields=[field]) is None
    assert catalogue.resolve("BSE", "MAIN", args[2]) is None
    assert catalogue.resolve("SZSE", "STAR", args[2]) is None


@pytest.mark.parametrize("exchange,board,minimum,step,maximum", [
    ("SSE", "MAIN", 100, 100, 1_000_000), ("SSE", "STAR", 200, 1, 100_000),
    ("SZSE", "MAIN", 100, 100, 1_000_000), ("SZSE", "CHINEXT", 100, 100, 300_000),
])
def test_reviewed_quantities_and_restricted_odd_lot_subset(
    catalogue, exchange, board, minimum, step, maximum,
):
    rule = catalogue.resolve(exchange, board, date(2026, 7, 6))
    assert (rule.buy_min_quantity, rule.buy_quantity_step, rule.max_limit_quantity) == (
        minimum, step, maximum,
    )
    assert rule.sellability_lag_sessions == 1
    assert rule.quantity_is_admissible(side=Side.SELL, quantity=99, total_position_quantity=99)
    assert not rule.quantity_is_admissible(side=Side.SELL, quantity=99, total_position_quantity=299)
    assert not rule.quantity_is_admissible(
        side=Side.BUY, quantity=maximum + step, total_position_quantity=0,
    )


@pytest.mark.parametrize("mutation", [
    "overlap", "duplicate", "future_publication", "past_supersession", "outside_review",
    "before_effective", "missing_provision", "unbound_provision", "missing_effective",
    "missing_supersession", "unknown_field", "unknown_scope", "authority", "negative",
    "boolean_quantity", "no_timezone", "unapproved_host",
])
def test_rejects_bad_or_ambiguous_evidence(inputs, mutation):
    c, m = copy.deepcopy(inputs)
    r = c["intervals"][0]
    if mutation == "overlap":
        new = copy.deepcopy(r)
        new["id"] += "-overlap"
        c["intervals"].append(new)
    elif mutation == "duplicate":
        c["intervals"].append(copy.deepcopy(r))
    elif mutation == "future_publication":
        m["documents"]["sse_2023_rules"]["published_on"] = "2023-04-11"
    elif mutation == "past_supersession":
        r["through"] = "2026-07-06"
    elif mutation == "outside_review":
        c["intervals"][1]["through"] = "2026-09-11"
    elif mutation == "before_effective":
        r["from"] = "2023-02-17"
    elif mutation == "missing_provision":
        r["provisions"]["price_tick"]["clause"] = ""
    elif mutation == "unbound_provision":
        r["provisions"]["price_tick"]["source_ids"] = ["szse_2026_rules"]
    elif mutation == "missing_effective":
        r["effective_evidence"] = []
    elif mutation == "missing_supersession":
        r["supersession_evidence"] = None
    elif mutation == "unknown_field":
        r["values"]["tradable"] = True
    elif mutation == "unknown_scope":
        r["board"] = "ETF"
    elif mutation == "authority":
        c["execution_authority"] = True
    elif mutation == "negative":
        r["values"]["buy_min_quantity"] = -100
    elif mutation == "boolean_quantity":
        r["values"]["buy_quantity_step"] = True
    elif mutation == "no_timezone":
        m["documents"]["sse_2023_rules"]["retrieved_at"] = "2026-09-11T10:00:00"
    elif mutation == "unapproved_host":
        m["documents"]["sse_2023_rules"]["url"] = "https://example.com/rules"
    with pytest.raises((DataValidationError, ExecutionValidationError)):
        EvidenceCatalogue(c, m)


def test_calendar_audit_counts_unknown_baseline_and_new_coverage(catalogue):
    days = [date(2023, 4, 7), date(2023, 4, 10), date(2025, 1, 2), date(2026, 7, 6)]
    report = audit_dates(catalogue, days, default_a_share_rule_book())
    assert len(report["rows"]) == 16
    assert sum(r["sessions"] for r in report["intervals"]) == 16
    for row in report["summary"]:
        assert row["catalogue_covered"] == 3
        assert row["still_unknown"] == 1
        assert row["newly_covered"] == (2 if row["exchange"] == "SSE" else 3)
        assert row["conflict"] == 0
    for invalid in ([], [days[0], days[0]], list(reversed(days)), [date(2022, 12, 30)]):
        with pytest.raises(DataValidationError):
            audit_dates(catalogue, invalid, default_a_share_rule_book())


def test_frozen_default_and_personal_sources_unchanged(catalogue, inputs):
    from quantlab.research.input_audit import _sha

    baseline = default_a_share_rule_book()
    assert baseline_fingerprint(baseline) == inputs[0]["baseline_fingerprint"]
    for name in ("src/quantlab/execution/rules.py", "src/quantlab/personal/quantity_rules.py"):
        assert _sha(ROOT / name) == inputs[1]["files"][name]["sha256"]
    assert baseline.resolve("SZSE", "MAIN", date(2024, 1, 2)) is None
    assert baseline.resolve("SSE", "MAIN", date(2026, 1, 2)) is None
    assert catalogue.resolve("SZSE", "MAIN", date(2024, 1, 2)) is not None
    # Preserve the disclosed old date bug instead of changing a frozen version silently.
    assert baseline.resolve("SZSE", "MAIN", date(2021, 3, 31)).version == "2021.03"


def test_byte_binding_rejects_modified_contract_before_reading_sources(tmp_path, inputs):
    (tmp_path / "config").mkdir()
    (tmp_path / CONFIG).write_text("{}")
    (tmp_path / MANIFEST).write_text(json.dumps(inputs[1]))
    with pytest.raises(DataValidationError, match="contract changed"):
        load_catalogue(tmp_path)


def test_disagreement_is_never_classified_as_new_coverage(inputs):
    c, m = copy.deepcopy(inputs)
    c["intervals"][0]["values"]["max_limit_quantity"] = 900_000
    report = audit_dates(EvidenceCatalogue(c, m), [date(2024, 1, 2)], default_a_share_rule_book())
    row = report["rows"][0]
    assert row["status"] == "conflict"
    assert row["differences"]["max_limit_quantity"] == {
        "baseline": 1_000_000, "catalogue": 900_000,
    }
    assert values(default_a_share_rule_book().resolve("SSE", "MAIN", date(2024, 1, 2)))[
        "max_limit_quantity"
    ] == 1_000_000
