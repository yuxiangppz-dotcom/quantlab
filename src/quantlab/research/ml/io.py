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
OPTIONAL_BUNDLE_FILES = ("pit_lineage.parquet", "feature_contract.json")


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
    from quantlab.research.ml.artifacts import atomic_json

    atomic_json(path, payload)


def seal_bundle(folder, *, provenance):
    if not isinstance(provenance, dict) or not provenance:
        raise ValueError("explicit input provenance is required")
    payload = {
        "schema": "quantlab_ml_inputs_v2",
        "provenance": provenance,
        "historical_data_certified": False,
        "files": {
            name: sha256(folder / name)
            for name in (*BUNDLE_FILES, *OPTIONAL_BUNDLE_FILES)
            if (folder / name).exists()
        },
    }
    write_json(folder / "manifest.json", payload)
    return payload


def verify_bundle(folder):
    payload = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    files = set(payload["files"])
    if (
        payload.get("schema") != "quantlab_ml_inputs_v2"
        or not set(BUNDLE_FILES).issubset(files)
        or files - set((*BUNDLE_FILES, *OPTIONAL_BUNDLE_FILES))
    ):
        raise ValueError("unexpected input bundle schema or files")
    for name in OPTIONAL_BUNDLE_FILES:
        if (folder / name).exists() != (name in files):
            raise ValueError("unbound optional input artifact")
    if ("pit_lineage.parquet" in files) != ("feature_contract.json" in files):
        raise ValueError("lineage and feature contract must be supplied together")
    for name in files:
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


class MarketDayStore:
    """JSONL offset index; decode one session rather than retaining every context."""

    def __init__(self, path):
        self.path = Path(path)
        self.fingerprint = sha256(self.path)
        self.offsets = {}
        with self.path.open("rb") as stream:
            while True:
                offset = stream.tell()
                line = stream.readline()
                if not line:
                    break
                if not line.strip():
                    continue
                raw = json.loads(line)
                day = date.fromisoformat(raw["session"])
                if day in self.offsets:
                    raise ValueError("duplicate market day")
                if raw.get("orders"):
                    raise ValueError("market evidence must not inject orders")
                self.offsets[day] = offset

    def get(self, day):
        offset = self.offsets.get(day)
        if offset is None:
            return None
        with self.path.open("rb") as stream:
            stream.seek(offset)
            raw = json.loads(stream.readline())
        if date.fromisoformat(raw["session"]) != day:
            raise ValueError("market input changed during indexed reading")
        return decode_market_day(raw)


def read_market_days(path):
    return MarketDayStore(path)


def read_corporate_actions(path, start, end):
    from quantlab.research.ml.corporate import decode_event

    payload = json.loads(Path(path).read_text())
    coverage = payload["coverage"]
    if (
        not isinstance(coverage.get("source_id"), str)
        or not coverage["source_id"].strip()
        or date.fromisoformat(coverage["start"]) > start
        or date.fromisoformat(coverage["end"]) < end
    ):
        raise ValueError("explicit corporate coverage/source required for the replay interval")
    events = tuple(decode_event(e) for e in payload["events"])
    if len({e.event_id for e in events}) != len(events):
        raise ValueError("duplicate corporate event id")
    # A legacy artifact could carry an executable first vendor row even after
    # its distribution group was flagged as a conflicting duplicate. Check the
    # entire artifact, including events outside this particular read window.
    conflicts = set()
    for item in coverage.get("unresolved", []):
        if item.get("reason") != "conflicting_duplicate":
            continue
        key = (item.get("instrument_id"), item.get("ex_date"), item.get("record_date"))
        if not all(key):
            raise ValueError("incomplete conflicting corporate group identity")
        conflicts.add(key)
    if any((e.instrument_id, str(e.ex_date), str(e.record_date)) in conflicts for e in events):
        raise ValueError("conflicting corporate event remains executable; rebuild evidence")
    return tuple(e for e in events if start <= e.ex_date <= end)
