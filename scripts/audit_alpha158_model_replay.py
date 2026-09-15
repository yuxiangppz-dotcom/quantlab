"""Audit the commissioned saved-score replay; no inferred fills or corporate cash."""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

import pandas as pd

from quantlab.data import ParquetStorage
from quantlab.data.index_context import load_index_context
from quantlab.research.model_replay_intake import (
    bound_scores,
    nav_metrics,
    rank_targets,
    raw_dividend_receipt,
)
from quantlab.research.round2_dataset import sealed_read
from quantlab.research.rule_evidence_v2 import load_catalogue


def run(root: Path, output: Path) -> dict:
    if output.resolve().is_relative_to((root / "data/canonical").resolve()):
        raise ValueError("canonical output is forbidden")
    output.mkdir(parents=True, exist_ok=False)
    manifest = {}

    def bind(path):
        raw = path.read_bytes()
        entry = {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
        key = str(path.resolve())
        if key in manifest and manifest[key] != entry:
            raise ValueError("source changed during audit")
        manifest[key] = entry
        return entry

    (output / "started.json").write_text(json.dumps({
        "scope": "saved_alpha158_lightgbm_replay_intake_only",
        "portfolio_metrics_authorized_but_not_available_without_admission": True,
    }))
    canonical = root / "data/canonical"
    store = ParquetStorage(canonical)
    for folder in ["calendar", "securities"]:
        for path in (canonical / folder).rglob("*.parquet"):
            bind(path)
    calendar = pd.DatetimeIndex(sorted({x.trade_date for x in store.load_trading_calendar()
                                       if x.is_open}))
    model_root = root / "data/products/alpha158_rolling/alpha158_rolling_20260911"
    bind(model_root / "report.json")
    report = sealed_read(model_root / "report.json")
    attempts = {row["slot"]: row for row in report["attempts"]}
    frames = []
    for fold, phase in [("fold1", "evaluation"), ("fold2", "evaluation"),
                        ("fold3", "evaluation"), ("fold3", "observed_2026")]:
        slot = fold + "_lightgbm"
        path = model_root / "fits" / slot / f"scores_{phase}.parquet"
        bind(path)
        frame = bound_scores(path, attempts[slot]["artifacts"][path.name])
        frame["model_slot"] = slot
        frames.append(frame)
    scores = pd.concat(frames, ignore_index=True)
    targets = rank_targets(scores, calendar)
    targets = targets.merge(scores[["instrument_id", "trade_date", "model_slot"]],
                            on=["instrument_id", "trade_date"], validate="one_to_one")
    targets.to_parquet(output / "targets.parquet", index=False)
    print(f"targets: {len(targets)} rows", flush=True)
    securities = {x.instrument_id: x for x in store.load_securities()}
    rule_book = load_catalogue(root)
    gaps = []
    st_rows = 0
    last_date = targets.trade_date.max()
    for count, (signal, group) in enumerate(targets.groupby("trade_date"), 1):
        execution = group.intended_execution_date.iloc[0]
        if pd.isna(execution) or execution > last_date:
            continue
        def load(kind, day):
            path = canonical / kind / f"year={day.year}/month={day.month:02d}/{day.date()}.parquet"
            if not path.exists():
                return None
            bind(path)
            return pd.read_parquet(path)
        daily = load("daily", execution)
        limits = load("daily_price_limit", execution)
        st = load("lifecycle_context_v1/stock_st", signal)
        suspensions = load("lifecycle_context_v1/suspensions", execution)
        for item in group.itertuples():
            code = item.instrument_id
            missing = []
            if st is not None and code in set(st.instrument_id):
                st_rows += 1
            for label, frame in [("execution_quote", daily), ("execution_limits", limits)]:
                if frame is None or not frame.instrument_id.eq(code).any():
                    missing.append(label + "_unavailable")
            if suspensions is None:
                missing.append("suspension_partition_missing")
            if st is None:
                missing.append("st_partition_missing")
            security = securities.get(code)
            if security is None:
                missing.append("security_metadata_missing")
            else:
                board = {"主板": "MAIN", "中小板": "MAIN", "创业板": "CHINEXT",
                         "科创板": "STAR"}.get(security.board)
                rule = rule_book.resolve(security.exchange, board, execution.date())
                if rule is None:
                    missing.append("authoritative_quantity_rule_unavailable")
            for reason in missing:
                gaps.append({"signal_date": str(signal.date()),
                             "execution_date": str(execution.date()),
                             "instrument_id": code, "reason": reason})
        if count % 200 == 0:
            print(f"execution input dates checked: {count}", flush=True)
    raw_root = root / "data/products/corporate_action_staging/dividend_targets_20260911/attempts"
    dividend_counts = Counter()
    for number, code in enumerate(sorted(set(targets.instrument_id)), 1):
        rows, sources = raw_dividend_receipt(raw_root / code, code)
        for source in sources:
            bind(source)
        dividend_counts["verified_raw_responses"] += 1
        dividend_counts["raw_rows"] += len(rows)
        if number % 1000 == 0:
            print(f"raw dividend responses verified: {number}", flush=True)
    snapshot = canonical / ("research_index_context_v1/"
                           "162764d675283a4eb8d42e1f621ea262fde9054b37a14bb6f4383a135bc623f2.json")
    bind(snapshot)
    index = load_index_context(
        snapshot, expected_fingerprint=
        "5fb766a57484af176254a8352d6ea54f14bcffb009a0854a379d5ff460497829",
    )
    frame = pd.DataFrame(next(s["rows"] for s in index["series"]
                              if s["metadata"]["ts_code"] == "000001.SH"))
    frame["trade_date"] = pd.to_datetime(frame.trade_date)
    closes = frame.set_index("trade_date").close.sort_index()
    dates = calendar[(calendar >= targets.trade_date.min()) & (calendar <= last_date)]
    closes = closes.reindex(dates)
    benchmark = nav_metrics(closes)
    years = []
    for year in sorted(set(dates.year)):
        part = dates[dates.year == year]
        start = dates[max(0, dates.get_loc(part[0]) - 1)]
        years.append({"year": year, **nav_metrics(closes.loc[start:part[-1]])})
    pd.DataFrame({"date": dates, "index_close": closes.values,
                  "nav": closes.values / closes.iloc[0]}).to_csv(
                      output / "benchmark.csv", index=False)
    pd.DataFrame(gaps).to_csv(output / "input_gaps.csv", index=False)
    result = {
        "status": "blocked_before_economic_replay", "model": "Alpha158 rolling LightGBM",
        "start": str(dates[0].date()), "end": str(dates[-1].date()),
        "initial_cash_cny": 200000, "target_rows": len(targets),
        "unique_target_instruments": int(targets.instrument_id.nunique()),
        "st_target_rows": st_rows, "raw_dividends": dict(dividend_counts),
        "gap_counts": dict(Counter(x["reason"] for x in gaps)),
        "first_input_gap": gaps[0] if gaps else None,
        "additional_blockers": [
            "corporate_record_date_entitlements_cash_receivables_tax_and_share_availability_not_integrated",
            "historical_identity_not_certified_by_current_security_metadata",
            "suspended_or_delisted_positions_require_explicit_verified_valuation_not_invented_quotes",
        ],
        "portfolio_net_return": None, "portfolio_max_drawdown": None,
        "portfolio_sharpe": None, "beat_sse_composite": None,
        "benchmark": {"id": "000001.SH", "type": "price_index_not_investable_fund",
                      "full_period": benchmark, "by_year": years},
        "economic_paths_started": 0, "provider_calls": 0, "model_fits": 0,
        "performance_claim": False,
    }
    # Ensure the input bytes still match the receipts used in this audit.
    for key in list(manifest):
        bind(Path(key))
    (output / "source_manifest.json").write_text(json.dumps(manifest, indent=2))
    (output / "report.json").write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    run(args.source_root.resolve(), args.output.resolve())
