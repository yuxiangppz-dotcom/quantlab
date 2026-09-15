"""Acquire only missing selected-stock dividend responses under direct commission."""

from __future__ import annotations

import argparse
import hashlib
import os
import time
from datetime import UTC, datetime
from pathlib import Path

from dotenv import dotenv_values

from quantlab.data.dividend_raw import FIELDS, WireClient, inspect_response
from quantlab.research.alpha158_store import atomic_seal
from quantlab.research.model_replay_data import ReplayData


def run(root, output):
    data = ReplayData(root)
    targets = data.targets(exclude_st=True)
    original = root / "data/products/corporate_action_staging/dividend_targets_20260911/attempts"
    codes = sorted(c for c in set(targets.instrument_id) if not (original / c).exists())
    if len(codes) > 100 or output.exists() or output.is_relative_to(root / "data/canonical"):
        raise ValueError("bounded missing-only acquisition needs a fresh noncanonical directory")
    token = os.environ.get("TUSHARE_TOKEN") or dotenv_values(root / ".env").get("TUSHARE_TOKEN")
    if not token:
        raise ValueError("Tushare credential unavailable")
    output.mkdir(parents=True)
    atomic_seal(
        output / "request.json",
        {
            "scope": "user_commission_alpha158_exst_missing_dividends",
            "codes": codes,
            "api_name": "dividend",
            "max_requests": len(codes),
            "max_response_bytes": 1048576,
            "execution_authority": False,
            "canonical_writes": False,
        },
    )
    client = WireClient(token, "https://api.tushare.pro")
    statuses = []
    try:
        for code in codes:
            path = output / "attempts" / code / "01"
            parameters = {
                "api_name": "dividend",
                "params": {"ts_code": code},
                "fields": ",".join(FIELDS),
            }
            intent = atomic_seal(
                path / "intent.json",
                {
                    "instrument_id": code,
                    "parameters": parameters,
                    "at": datetime.now(UTC).isoformat(),
                },
            )
            raw, transport = client.fetch(parameters, 1048576)
            with (path / "response.body").open("xb") as handle:
                handle.write(raw)
            inspected = (
                inspect_response(raw, code)
                if transport["transport_status"] == "received"
                else {"status": transport["transport_status"], "rows": None}
            )
            receipt = atomic_seal(
                path / "result.json",
                {
                    **transport,
                    **inspected,
                    "intent_fingerprint": intent["fingerprint"],
                    "observed_at": datetime.now(UTC).isoformat(),
                    "artifacts": {
                        "response.body": {
                            "sha256": hashlib.sha256(raw).hexdigest(),
                            "bytes": len(raw),
                        }
                    },
                    "complete_event_history_certified": False,
                    "execution_authority": False,
                },
            )
            statuses.append({"code": code, "status": receipt["status"]})
            print(f"{len(statuses)}/{len(codes)} {code} {receipt['status']}", flush=True)
            if receipt["status"] not in {"nonempty", "empty"}:
                break
            time.sleep(0.5)
    finally:
        client.close()
        atomic_seal(
            output / "summary.json",
            {
                "attempts": statuses,
                "complete": len(statuses) == len(codes)
                and all(x["status"] in {"empty", "nonempty"} for x in statuses),
            },
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        run(args.source_root.resolve(), args.output.resolve())
    except Exception as exc:
        print(f"acquisition_failed:{type(exc).__name__}", flush=True)
        raise SystemExit(1) from None
