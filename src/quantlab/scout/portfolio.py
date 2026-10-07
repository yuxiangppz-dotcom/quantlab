"""Independent read-only holdings/watchlist observation, outside new-pick quotas."""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path

from quantlab.data.storage import ParquetStorage
from quantlab.scout.models import SHANGHAI, Candidate, timestamp

CODE = re.compile(r"^\d{6}\.(SH|SZ)$")


def load_portfolio_review(
    path: Path | None,
    now: datetime,
    session: date,
    storage: ParquetStorage,
    universe: dict[str, Candidate],
) -> dict:
    if path is None:
        return {"status": "not_provided", "rows": []}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or set(payload) != {"observed_at", "holdings", "watchlist"}:
        raise ValueError("Portfolio file requires observed_at, holdings and watchlist")
    observed = timestamp(payload["observed_at"])
    if observed > now:
        raise ValueError("Portfolio input is future-dated")
    codes: list[tuple[str, str]] = []
    for group in ("holdings", "watchlist"):
        values = payload[group]
        if not isinstance(values, list) or len(values) > 50:
            raise ValueError("Portfolio group must be a list of at most 50 stocks")
        for code in values:
            if not isinstance(code, str) or not CODE.fullmatch(code):
                raise ValueError("Invalid portfolio stock code")
            if code not in {existing for existing, _ in codes}:
                codes.append((code, group))
    master = {item.instrument_id: item for item in storage.load_securities()}
    bars = {item.instrument_id: item for item in storage.load_daily_bars_by_date(session)}
    rows = []
    for code, group in codes:
        security = master.get(code)
        bar = bars.get(code)
        concerns = []
        if security is None:
            concerns.append("security_identity_unknown")
        else:
            if security.list_status != "L":
                concerns.append("security_not_currently_listed")
            if "ST" in security.name.upper() or "退" in security.name:
                concerns.append("current_name_risk_warning")
            if security.board != "主板":
                concerns.append("outside_default_new_candidate_board")
        if bar is None:
            concerns.append("session_bar_missing_halt_or_data_gap_unknown")
        if code not in universe:
            concerns.append("outside_new_candidate_pool_reason_requires_review")
        rows.append(
            {
                "instrument_id": code,
                "group": group,
                "name": security.name if security else None,
                "asof_session": session.isoformat(),
                "close": bar.close if bar else None,
                "new_candidate_eligible": code in universe,
                "metrics": universe[code].metrics if code in universe else None,
                "concerns": concerns,
                "analysis_status": "program_facts_only_not_ai_deep_reviewed",
            }
        )
    return {
        "status": "provided",
        "observed_at": observed.astimezone(SHANGHAI).isoformat(),
        "stale_input": (now - observed).total_seconds() > 7 * 86400,
        "rows": rows,
        "note": (
            "Holdings/watchlist are separate from new-pick quotas; no trade advice or order state"
        ),
    }
