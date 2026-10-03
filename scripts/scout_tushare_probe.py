"""Probe bounded TuShare Scout sources without exposing credentials or raw rows."""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime
from pathlib import Path

from quantlab.scout.models import SHANGHAI

DOC = "https://tushare.pro/document/2?doc_id="
SPECS = (
    ("limit_list_d", 298, 5000, {"trade_date": "{day}"}, 2500),
    ("kpl_list", 347, 5000, {"trade_date": "{day}", "tag": "涨停"}, 8000),
    ("kpl_concept_cons", 351, 5000, {"trade_date": "{day}"}, 3000),
    ("forecast_vip", 45, 5000, {"period": "20260630"}, None),
    ("express_vip", 46, 5000, {"period": "20260630"}, None),
    ("fina_indicator", 79, 2000, {"ts_code": "000011.SZ", "period": "20260630"}, None),
    ("fina_mainbz", 81, 2000, {"ts_code": "000011.SZ", "period": "20260630"}, None),
    ("repurchase", 124, 2000, {"start_date": "20260901", "end_date": "{day}"}, None),
    ("stk_holdertrade", 175, 2000, {"start_date": "20260901", "end_date": "{day}"}, None),
    ("disclosure_date", 162, 5000, {"ts_code": "000011.SZ", "end_date": "20260930"}, None),
    ("share_float", 160, 5000, {"start_date": "20261001", "end_date": "20261031"}, 6000),
    ("suspend_d", 214, 5000, {"trade_date": "{day}"}, None),
    ("moneyflow", 170, 2000, {"ts_code": "000011.SZ", "trade_date": "{day}"}, None),
    ("index_classify", 181, 2000, {"level": "L1", "src": "SW2021"}, None),
    ("index_member_all", 335, 2000, {"l1_code": "801010.SI"}, 2000),
)


def classify_error(exc: Exception) -> str:
    message = str(exc).lower()
    if any(term in message for term in ("权限", "积分", "permission", "access denied")):
        return "permission_denied"
    if any(term in message for term in ("频次", "限流", "rate limit", "too many")):
        return "rate_limited"
    if any(term in message for term in ("timeout", "connection", "network", "ssl")):
        return "network_error"
    return "provider_error"


def probe(output: Path, session: str) -> dict:
    token = os.environ.get("TUSHARE_TOKEN")
    if not token:
        raise RuntimeError("TUSHARE_TOKEN is missing")
    if output.exists():
        raise FileExistsError("Refusing to overwrite an existing interface matrix")
    import tushare as ts

    client = ts.pro_api(token, timeout=20)
    now = datetime.now(SHANGHAI)
    records = []
    for api, doc_id, threshold, template, cap in SPECS:
        params = {name: value.replace("{day}", session) for name, value in template.items()}
        started = time.monotonic()
        row = {
            "api": api,
            "doc_url": DOC + str(doc_id),
            "doc_checked_date": now.date().isoformat(),
            "documented_points": threshold,
            "params": params,
            "probed_at": datetime.now(SHANGHAI).isoformat(),
        }
        try:
            frame = getattr(client, api)(**params)
            row.update(
                {
                    "status": "possibly_truncated"
                    if cap and len(frame) >= cap
                    else "data"
                    if len(frame)
                    else "empty_unconfirmed",
                    "row_count": len(frame),
                    "returned_fields": list(frame.columns),
                    "sample_dates": {
                        key: sorted({str(v) for v in frame[key].dropna().head(30)})[-3:]
                        for key in ("trade_date", "ann_date", "end_date", "period")
                        if key in frame
                    },
                    "row_cap": cap,
                }
            )
        except Exception as exc:  # Probe records status, never provider error text.
            row.update({"status": classify_error(exc), "row_count": None, "returned_fields": []})
        row["elapsed_ms"] = round((time.monotonic() - started) * 1000)
        records.append(row)
    result = {"created_at": now.isoformat(), "session": session, "probes": records}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", required=True, help="Completed session YYYYMMDD")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = probe(args.output, args.session)
    print(json.dumps({row["api"]: (row["status"], row["row_count"]) for row in result["probes"]}))
