"""Read-only saved-score admission; never equate target rows with filled holdings."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from quantlab.data.models import DataValidationError
from quantlab.research.round2_dataset import sealed_read


def bound_scores(path: Path, receipt: dict) -> pd.DataFrame:
    raw = path.read_bytes()
    if (len(raw) != receipt["bytes"]
            or hashlib.sha256(raw).hexdigest() != receipt["sha256"]):
        raise DataValidationError("saved model score receipt mismatch")
    # Labels and future-label masks must never determine target membership.
    return pd.read_parquet(path, columns=["instrument_id", "trade_date", "score"])


def rank_targets(scores: pd.DataFrame, calendar: pd.DatetimeIndex) -> pd.DataFrame:
    if calendar.has_duplicates or not calendar.is_monotonic_increasing:
        raise DataValidationError("calendar must be unique and ordered")
    work = scores[["instrument_id", "trade_date", "score"]].copy()
    work["trade_date"] = pd.to_datetime(work["trade_date"], errors="raise")
    if work.duplicated(["instrument_id", "trade_date"]).any():
        raise DataValidationError("duplicate score identity")
    if not work.trade_date.isin(calendar).all():
        raise DataValidationError("score date outside calendar")
    if work.instrument_id.isna().any() or work.instrument_id.astype(str).str.strip().eq("").any():
        raise DataValidationError("score instrument missing")
    work["score"] = pd.to_numeric(work.score, errors="raise")
    work = work[np.isfinite(work.score)].sort_values(
        ["trade_date", "score", "instrument_id"], ascending=[True, False, True], kind="stable"
    )
    work = work.groupby("trade_date", sort=True).head(20).copy()
    work["rank"] = work.groupby("trade_date").cumcount() + 1
    work["target_weight"] = 0.05
    next_dates = dict(zip(calendar[:-1], calendar[1:], strict=True))
    work["intended_execution_date"] = work.trade_date.map(next_dates)
    work["cash_weight"] = 1 - work.groupby("trade_date").target_weight.transform("sum")
    return work.reset_index(drop=True)


def nav_metrics(nav: pd.Series) -> dict:
    if len(nav) < 2 or nav.index.has_duplicates or not nav.index.is_monotonic_increasing:
        raise DataValidationError("NAV needs ordered unique dates and at least two observations")
    values = nav.to_numpy(dtype=float)
    if not np.isfinite(values).all() or (values <= 0).any():
        raise DataValidationError("NAV has missing or nonpositive values")
    returns = nav.pct_change(fill_method=None).iloc[1:]
    std = float(returns.std(ddof=1))
    total = float(values[-1] / values[0] - 1)
    return {
        "start": str(nav.index[0].date()), "end": str(nav.index[-1].date()),
        "return_intervals": len(returns), "total_return": total,
        "cagr_252": (1 + total) ** (252 / len(returns)) - 1,
        "max_drawdown": float((nav / nav.cummax() - 1).min()),
        "sharpe_rf0_252": (float(returns.mean()) / std * np.sqrt(252)
                           if len(returns) > 1 and std > 0 else None),
        "risk_free_rate": 0, "annualization_sessions": 252,
    }


def raw_dividend_receipt(attempt_dir: Path, code: str) -> tuple[list[dict], list[Path]]:
    candidates = []
    for folder in sorted(attempt_dir.iterdir()):
        path = folder / "result.json"
        if not path.exists():
            continue
        receipt = sealed_read(path)
        if receipt["status"] not in {"nonempty", "empty"}:
            continue
        intent = sealed_read(folder / "intent.json")
        if receipt["intent_fingerprint"] != intent["fingerprint"]:
            raise DataValidationError("dividend intent binding mismatch")
        if intent["instrument_id"] != code:
            raise DataValidationError("dividend instrument mismatch")
        body_path = folder / "response.body"
        raw = body_path.read_bytes()
        artifact = receipt["artifacts"]["response.body"]
        if (hashlib.sha256(raw).hexdigest() != receipt["wire_sha256"]
                or artifact["sha256"] != receipt["wire_sha256"]
                or len(raw) != artifact["bytes"] or receipt["http_status"] != 200):
            raise DataValidationError("dividend response binding mismatch")
        body = json.loads(raw)
        if body["code"] != 0:
            raise DataValidationError("dividend server response failed")
        names, items = body["data"]["fields"], body["data"]["items"]
        if len(set(names)) != len(names) or len(items) != receipt["rows"]:
            raise DataValidationError("dividend rows/fields changed")
        rows = [dict(zip(names, row, strict=True)) for row in items]
        if any(row["ts_code"] != code for row in rows):
            raise DataValidationError("dividend response has another instrument")
        candidates.append((rows, [path, folder / "intent.json", body_path]))
    if not candidates:
        raise DataValidationError("no verified raw dividend response")
    # Later successful snapshots cannot silently replace contradictory earlier ones.
    if any(rows != candidates[-1][0] for rows, _ in candidates[:-1]):
        raise DataValidationError("conflicting successful dividend snapshots")
    return candidates[-1]
