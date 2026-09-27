"""Recover mature labels only for signals actually used by the paper account."""

import json

import pandas as pd

from quantlab.data.storage import ParquetStorage
from quantlab.pipeline.ingestion import verify_session
from quantlab.research.ml import service
from quantlab.research.ml.artifacts import checkpoint_read, verify_completed, verify_publication
from quantlab.research.ml.data import calendar_index, execution_labels
from quantlab.research.ml.evaluation import signal_diagnostics
from quantlab.research.ml.io import sha256
from quantlab.research.ml.reporting import block_mean_interval


def mature_panel(scores, prices, sessions, config, asof):
    scores = scores.copy()
    scores["trade_date"] = pd.to_datetime(scores.trade_date)
    # Preserve score-day keys even when that day's price is missing. Maturity is
    # calendar based; missing endpoints must reduce coverage, not vanish.
    keys = scores[["trade_date", "instrument_id"]].drop_duplicates()
    prices = pd.concat([prices, keys], ignore_index=True).drop_duplicates(
        ["trade_date", "instrument_id"], keep="first"
    )
    labels = execution_labels(prices, sessions, config)
    joined = scores.merge(
        labels, on=["trade_date", "instrument_id"], how="left", validate="many_to_one"
    )
    return joined.loc[joined.label_end.le(pd.Timestamp(asof))]


def monitor(project, asof):
    root = project["account"]
    metadata = service.load_service(root)
    head, _, _ = service.account_head(root, metadata)
    if asof > head["book"].asof_date:
        raise ValueError("monitoring cannot extend beyond the settled account")
    config = service.config_from(metadata["config"])
    storage = ParquetStorage(project["canonical"])
    sessions = calendar_index(
        sorted({c.trade_date for c in storage.load_trading_calendar() if c.is_open})
    )
    past = sessions[sessions <= pd.Timestamp(asof)]
    first = past[max(0, len(past) - 60 - config.horizon_sessions - 1)].strftime("%Y-%m-%d")
    scores, drift, bindings = [], [], {}
    for folder in sorted((root / "sessions").iterdir()):
        if not folder.is_dir() or not first <= folder.name <= str(asof):
            continue
        saved = checkpoint_read(folder / "account.json")
        saved, decision_folder = service.resolved_decision(
            root, saved["state"]["book"].asof_date, saved
        )
        publication = verify_publication(
            decision_folder, saved["state"]["book"].asof_date, require_forward=False
        )
        if (
            saved["plan"] is None
            or saved["status"] != "decision_ready"
            or not publication["forward_eligible"]
        ):
            continue
        candidates = [
            root / "signals" / folder.name,
            *sorted((root / "retry_signals" / folder.name).glob("*")),
        ]
        matches = [
            p
            for p in candidates
            if (p / "completed.json").exists()
            and sha256(p / "completed.json") == saved["signal_sha256"]
        ]
        if len(matches) != 1:
            raise ValueError("account's published signal cannot be resolved uniquely")
        signal = matches[0]
        verify_completed(signal)
        verify_publication(signal, saved["state"]["book"].asof_date)
        bindings[str(signal / "completed.json")] = saved["signal_sha256"]
        scores.append(pd.read_parquet(signal / "scores.parquet"))
        prediction = json.loads((signal / "prediction.json").read_text())
        drift.append(
            {
                "session": folder.name,
                "model_id": prediction["model_id"],
                "alert_features": sum(v["alert"] for v in prediction["drift"].values()),
                "features": len(prediction["drift"]),
            }
        )
    if not scores:
        return {
            "status": "insufficient_history",
            "mature_signal_days": 0,
            "reason": "no qualified forward account signals",
        }
    scores = pd.concat(scores, ignore_index=True)
    start = pd.to_datetime(scores.trade_date).min()
    instruments = set(scores.instrument_id)
    prices = []
    for timestamp in sessions[(sessions >= start) & (sessions <= pd.Timestamp(asof))]:
        day = timestamp.date()
        verify_session(storage, project["receipts"], day)
        bindings[str(project["receipts"] / "sessions" / f"{day}.json")] = sha256(
            project["receipts"] / "sessions" / f"{day}.json"
        )
        factors = {f.instrument_id: f.adj_factor for f in storage.load_adj_factors_by_date(day)}
        prices.extend(
            {
                "trade_date": timestamp,
                "instrument_id": b.instrument_id,
                "adj_close": b.close * factors.get(b.instrument_id, float("nan")),
            }
            for b in storage.load_daily_bars_by_date(day)
            if b.instrument_id in instruments
        )
    mature = mature_panel(
        scores,
        pd.DataFrame(prices, columns=["trade_date", "instrument_id", "adj_close"]),
        sessions,
        config,
        asof,
    )
    if mature.empty:
        return {"status": "insufficient_history", "mature_signal_days": 0, "drift": drift}
    daily, summaries = signal_diagnostics(mature, config.min_cross_section)
    uncertainty = {
        model: block_mean_interval(
            group.sort_values("trade_date").rank_ic.tail(60), config.horizon_sessions + 1
        )
        for model, group in daily.groupby("model")
    }
    alerts = [
        f"negative mature rank IC:{model}"
        for model, item in uncertainty.items()
        if item.get("status") == "computed" and item["upper_95"] < 0
    ]
    if drift and drift[-1]["alert_features"] > 0.2 * drift[-1]["features"]:
        alerts.append(
            "more than 20% of feature means shifted beyond three training standard deviations"
        )
    if any(summary["mean_label_coverage"] < 0.95 for summary in summaries.values()):
        alerts.append("mature label coverage below 95%; investigate missing endpoints")
    return {
        "status": "alert" if alerts else "observing",
        "asof": str(asof),
        "mature_signal_days": int(mature.trade_date.nunique()),
        "summary": summaries,
        "recent_uncertainty": uncertainty,
        "drift": drift,
        "alerts": alerts,
        "inputs": bindings,
        "action": "review candidate/data before a new release"
        if alerts
        else "continue observation",
        "performance_certified": False,
        "account_mutated": False,
    }
