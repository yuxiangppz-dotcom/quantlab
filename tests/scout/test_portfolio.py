"""Holdings remain visible even when outside the new-candidate pool."""

import json
from datetime import datetime

from quantlab.data.storage import ParquetStorage
from quantlab.scout.demo import make_demo_market
from quantlab.scout.market import scan_market
from quantlab.scout.models import SHANGHAI
from quantlab.scout.portfolio import load_portfolio_review


def test_holdings_do_not_disappear_when_identity_or_bar_is_unknown(tmp_path):
    canonical = tmp_path / "canonical"
    day = make_demo_market(canonical)
    storage = ParquetStorage(canonical)
    universe, _ = scan_market(canonical, day)
    path = tmp_path / "portfolio.json"
    path.write_text(
        json.dumps(
            {
                "observed_at": datetime(2026, 9, 30, 8, tzinfo=SHANGHAI).isoformat(),
                "holdings": ["600007.SH", "999999.SH"],
                "watchlist": ["600007.SH"],
            }
        ),
        encoding="utf-8",
    )
    review = load_portfolio_review(
        path, datetime(2026, 9, 30, 9, tzinfo=SHANGHAI), day, storage, universe
    )
    assert len(review["rows"]) == 2
    assert review["rows"][0]["group"] == "holdings"
    assert review["rows"][1]["instrument_id"] == "999999.SH"
    assert "security_identity_unknown" in review["rows"][1]["concerns"]
    assert "session_bar_missing_halt_or_data_gap_unknown" in review["rows"][1]["concerns"]
