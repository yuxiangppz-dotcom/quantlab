import copy
import json
from dataclasses import asdict
from datetime import date
from pathlib import Path

import pytest

from quantlab.data.models import DataValidationError
from quantlab.execution.models import OrderSession, OrderType, Side
from quantlab.research.closing_quantity import CONFIG, ClosingQuantityCatalogue
from quantlab.research.rule_evidence import SCOPES, EvidenceCatalogue, values
from quantlab.research.rule_evidence_v3 import EarlierCatalogue

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def catalogue():
    def read(path):
        return json.loads((ROOT / path).read_text(encoding="utf-8"))

    parent = EvidenceCatalogue(
        read("config/historical_rule_catalogue_v2.json"),
        read("config/historical_rule_sources_v2.json"),
    )
    return ClosingQuantityCatalogue(
        read(CONFIG), EarlierCatalogue(read("config/historical_rule_catalogue_v3.json"), parent)
    )


@pytest.mark.parametrize("exchange,board", SCOPES)
@pytest.mark.parametrize(
    "day",
    [date(2022, 1, 4), date(2022, 9, 5), date(2023, 4, 9), date(2023, 4, 10), date(2024, 12, 31)],
)
def test_closing_extension_preserves_numbers_sources_and_old_queries(
    catalogue, exchange, board, day
):
    before = catalogue.parent.resolve(exchange, board, day)
    assert asdict(catalogue.resolve(exchange, board, day)) == asdict(before)
    after = catalogue.resolve(exchange, board, day, session=OrderSession.CLOSING_AUCTION)
    assert values(after) == values(before)
    assert after.sources == before.sources
    assert (after.effective_from, after.effective_to) == (
        before.effective_from,
        before.effective_to,
    )
    assert after.supported_sessions == (OrderSession.CLOSING_AUCTION,)
    assert after.limitations[: len(before.limitations)] == before.limitations
    assert (
        catalogue.parent.resolve(exchange, board, day, session=OrderSession.CLOSING_AUCTION) is None
    )
    assert after.quantity_is_admissible(side=Side.SELL, quantity=99, total_position_quantity=99)
    assert not after.quantity_is_admissible(side=Side.BUY, quantity=99, total_position_quantity=0)
    assert not after.quantity_is_admissible(
        side=Side.SELL, quantity=99, total_position_quantity=300
    )
    assert not after.quantity_is_admissible(
        side=Side.BUY, quantity=after.max_limit_quantity + 1, total_position_quantity=0
    )


@pytest.mark.parametrize("day", [date(2021, 12, 31), date(2025, 1, 1), date(2026, 9, 11)])
def test_closing_does_not_extrapolate(catalogue, day):
    assert catalogue.resolve("SSE", "MAIN", day, session=OrderSession.CLOSING_AUCTION) is None
    assert catalogue.resolve("SSE", "MAIN", day) == catalogue.parent.resolve("SSE", "MAIN", day)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"fields": ("price_limit",)},
        {"order_type": OrderType.MARKET},
        {"session": OrderSession.OPENING_AUCTION},
        {"session": "after_hours"},
    ],
)
def test_unsupported_requests_stay_unknown(catalogue, kwargs):
    request = {"session": OrderSession.CLOSING_AUCTION, **kwargs}
    assert catalogue.resolve("SSE", "MAIN", date(2022, 1, 4), **request) is None


@pytest.mark.parametrize("exchange,board", [("SSE", "ETF"), ("BSE", "MAIN"), ("SZSE", "STAR")])
def test_unsupported_identity(catalogue, exchange, board):
    assert (
        catalogue.resolve(exchange, board, date(2022, 1, 4), session=OrderSession.CLOSING_AUCTION)
        is None
    )


@pytest.mark.parametrize(
    "changed",
    [
        "authority",
        "performance",
        "dates",
        "fields",
        "parent",
        "missing_interval",
        "source",
        "clause",
        "limitations",
    ],
)
def test_changed_contract_rejected(catalogue, changed):
    c = copy.deepcopy(catalogue.contract)
    if changed == "authority":
        c["execution_authority"] = True
    elif changed == "performance":
        c["performance_evidence"] = True
    elif changed == "dates":
        c["audit_interval"][0] = "2020-01-01"
    elif changed == "fields":
        c["fields"].append("price_limit")
    elif changed == "parent":
        c["parent_contract_fingerprint"] = "0" * 64
    elif changed == "missing_interval":
        c["provisions"].pop(next(iter(c["provisions"])))
    elif changed in ("source", "clause"):
        ref = next(iter(c["provisions"].values()))[0]
        ref["source_id" if changed == "source" else "clause"] = (
            "unreviewed" if changed == "source" else ""
        )
    else:
        c["limitations"] = []
    with pytest.raises(DataValidationError):
        ClosingQuantityCatalogue(c, catalogue.parent)
