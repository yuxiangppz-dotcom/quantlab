"""Immutable input/output binding and typed scenario decoding; no provider calls."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from quantlab.research.quantity_kernel import (
    ResearchFeeScenario,
    ResearchQuantityRules,
    ResearchSession,
)
from quantlab.research.quantity_scheduler import RawCloseMark, ResearchDay

BUNDLE_FILES = ("features.parquet", "prices.parquet", "calendar.json", "feature_names.json")


def research_output(path):
    if {"canonical", "raw"} & set(Path(path).resolve().parts):
        raise ValueError("research output must not write canonical/raw source directories")
    return Path(path)


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path, payload):
    # Exclusive create: a rerun cannot silently replace previous evidence.
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write("\n")


def seal_bundle(folder, *, provenance):
    if not isinstance(provenance, dict) or not provenance:
        raise ValueError("explicit input provenance is required")
    payload = {
        "schema": "quantlab_ml_inputs_v2",
        "provenance": provenance,
        "historical_data_certified": False,
        "files": {name: sha256(folder / name) for name in BUNDLE_FILES},
    }
    write_json(folder / "manifest.json", payload)
    return payload


def verify_bundle(folder):
    payload = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    if payload.get("schema") != "quantlab_ml_inputs_v2" or set(payload["files"]) != set(
        BUNDLE_FILES
    ):
        raise ValueError("unexpected input bundle schema or files")
    for name in BUNDLE_FILES:
        if sha256(folder / name) != payload["files"][name]:
            raise ValueError(f"input fingerprint changed:{name}")
    return payload


def _dates(payload, keys):
    result = dict(payload)
    for key in keys:
        if result.get(key) is not None:
            result[key] = date.fromisoformat(result[key])
    return result


def decode_market_day(payload):
    """JSON schema is the existing typed kernel, with Decimal rates as strings."""
    day = date.fromisoformat(payload["session"])
    contexts = []
    for raw in payload["contexts"]:
        item = _dates(raw, ("execution_date", "next_session", "evidence_date", "prior20_asof"))
        if item.get("participation") is not None:
            item["participation"] = Decimal(str(item["participation"]))
        if item.get("rules") is not None:
            item["rules"] = ResearchQuantityRules(
                **_dates(item["rules"], ("effective_from", "effective_through"))
            )
        if item.get("fees") is not None:
            fees = _dates(item["fees"], ("effective_from", "effective_through"))
            for key in (
                "commission_rate",
                "buy_stamp_rate",
                "sell_stamp_rate",
                "additional_fee_rate",
                "adverse_slippage_rate",
            ):
                if fees.get(key) is not None:
                    fees[key] = Decimal(str(fees[key]))
            item["fees"] = ResearchFeeScenario(**fees)
        contexts.append(ResearchSession(**item))
    marks = tuple(RawCloseMark(m["instrument_id"], day, m["price_fen"]) for m in payload["marks"])
    return ResearchDay(day, (), tuple(contexts), marks, payload["corporate_processing_complete"])


def read_market_days(path):
    with Path(path).open(encoding="utf-8") as stream:
        return tuple(decode_market_day(json.loads(line)) for line in stream if line.strip())
