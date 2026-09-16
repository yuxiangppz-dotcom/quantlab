from dataclasses import replace
from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from quantlab.research.ml.corporate import (
    CorporateEvent,
    apply_events,
    capture_entitlements,
    new_state,
    receivable_value,
)
from quantlab.research.quantity_kernel import ResearchBook, ResearchLot


def test_dividend_entitlement_survives_sale_and_receivable_precedes_cash():
    record, ex, pay = date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 5)
    event = CorporateEvent(
        "d",
        "A",
        "cash_dividend",
        record,
        ex,
        pay,
        "synthetic",
        net_cash_per_share_fen=Decimal("10"),
    )
    held = ResearchBook(record, 1000, (ResearchLot("a", "A", 100, date(2024, 1, 1), record),))
    state = capture_entitlements(new_state(), held, [event])
    # The entitlement belongs to the record-date holder, not the payment-date holder.
    sold = replace(held, lots=())
    book, state, _ = apply_events(sold, ex, [event], state, date(2024, 1, 1))
    assert book.cash_fen == 1000
    assert receivable_value(state) == 1000
    book, state, _ = apply_events(book, pay, [event], state, date(2024, 1, 1))
    assert book.cash_fen == 2000
    assert receivable_value(state) == 0
    book2, _, _ = apply_events(book, pay, [event], state, date(2024, 1, 1))
    assert book2 == book  # payment is not duplicated


def test_bonus_shares_are_locked_until_explicit_settlement():
    record, ex, pay = date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 8)
    event = CorporateEvent(
        "b",
        "A",
        "bonus_shares",
        record,
        ex,
        pay,
        "synthetic",
        share_numerator=1,
        share_denominator=10,
    )
    held = ResearchBook(record, 1000, (ResearchLot("a", "A", 100, date(2024, 1, 1), record),))
    state = capture_entitlements(new_state(), held, [event])
    book, _, _ = apply_events(held, ex, [event], state, date(2024, 1, 1))
    assert sum(lot.quantity for lot in book.lots) == 110
    assert book.lots[-1].sellable_on == pay
    bad = replace(event, share_denominator=3)
    with pytest.raises(ValueError, match="fractional"):
        apply_events(held, ex, [bad], state, date(2024, 1, 1))


def test_atomic_json_failure_leaves_no_published_partial(tmp_path):
    from quantlab.research.ml.io import write_json

    target = tmp_path / "artifact.json"
    with pytest.raises(ValueError):
        write_json(target, {"invalid": float("nan")})
    assert not target.exists()
    write_json(target, {"valid": 1})
    with pytest.raises(FileExistsError):
        write_json(target, {"valid": 2})


def test_checkpoint_codec_roundtrip_and_corruption(tmp_path):
    import json

    from quantlab.research.ml.artifacts import checkpoint_read, checkpoint_write

    book = ResearchBook(
        date(2024, 1, 2), 1200, (ResearchLot("a", "A", 100, date(2024, 1, 1), date(2024, 1, 2)),)
    )
    path = tmp_path / "checkpoint.json"
    checkpoint_write(path, {"book": book, "attempted": {"one"}})
    assert checkpoint_read(path) == {"book": book, "attempted": frozenset({"one"})}
    raw = json.loads(path.read_text())
    raw["payload"]["book"]["fields"]["cash_fen"] = 999
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="checksum"):
        checkpoint_read(path)


def test_arrow_reader_selects_only_requested_window(tmp_path):
    from quantlab.research.ml.panel import read_range

    frame = pd.DataFrame({"trade_date": pd.bdate_range("2024-01-01", periods=20), "x": range(20)})
    path = tmp_path / "panel.parquet"
    frame.to_parquet(path, row_group_size=5)
    read = read_range(path, frame.trade_date[4], frame.trade_date[6], max_bytes=3000)
    assert read.x.tolist() == [4, 5, 6]
