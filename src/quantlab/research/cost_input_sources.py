"""Pinned inputs for a saved-score audit, without loading or fitting a model."""

from __future__ import annotations

import json
from datetime import date

from quantlab.data.models import DataValidationError
from quantlab.data.storage import ParquetStorage
from quantlab.research.alpha158_rolling_protocol import HISTORY
from quantlab.research.alpha158_rolling_protocol import OUTPUT as ROLLING
from quantlab.research.input_audit import _sha
from quantlab.research.round2_dataset import sealed_read, verify_entries
from quantlab.research.rule_evidence_v2 import read_report as rule_report
from quantlab.research.signal_feasibility import verify_historical_inputs

CONFIG = "config/cost_input_audit_v1.json"
MANIFEST = "config/cost_input_sources_v1.json"
OUTPUT = "data/products/cost_input_audit/cost_inputs_20260911"


def source_paths(root, config):
    """Enumerate the fixed calendar, including absent required daily partitions."""
    inventory = sealed_read(root / HISTORY / "inventory.json")
    if inventory["fingerprint"] != config["calendar_fingerprint"]:
        raise DataValidationError("cost audit calendar identity changed")
    sessions = [s for s in inventory["sessions"] if config["start"] <= s <= config["end"]]
    if len(sessions) != config["expected_sessions"] or len(set(sessions)) != len(sessions):
        raise DataValidationError("cost audit calendar population changed")
    store = ParquetStorage(root / "data/canonical")
    groups = {
        kind: [fn(date.fromisoformat(s)).relative_to(root).as_posix() for s in sessions]
        for kind, fn in (("daily", store.daily_bars_path), ("adj_factor", store.adj_factor_path))
    }
    groups["dividend"] = [
        p.relative_to(root).as_posix()
        for p in sorted((root / "data/canonical/dividend").rglob("*.parquet"))
    ]
    return inventory, sessions, groups


def verify_rolling(root, config):
    out = root / ROLLING
    report, plan = (sealed_read(out / n) for n in ("report.json", "plan.json"))
    if (
        report["fingerprint"] != config["rolling_report"]
        or report["identity"] != plan["fingerprint"]
        or plan["code_head"] != config["rolling_source_head"]
        or report["completed_fits"] != 6
        or report["cumulative_fit_attempts"] != 6
    ):
        raise DataValidationError("rolling source identity or six-fit status changed")
    historical = verify_historical_inputs(
        root, {"code_head": plan["code_head"], "inputs": plan["code_files"]}
    )
    verify_entries(root, plan["source"]["files"])
    metadata = sealed_read(out / "metadata.json")
    if metadata["identity"] != plan["fingerprint"]:
        raise DataValidationError("saved metadata identity changed")
    verify_entries(out, metadata["artifacts"])
    files, slots = [], []
    for result in report["attempts"]:
        slot = result["slot"]
        folder = out / "fits" / slot
        actual = sealed_read(folder / "result.json")
        if (
            actual != result
            or result["status"] != "completed"
            or result["identity"] != plan["fingerprint"]
        ):
            raise DataValidationError("saved fit receipt changed")
        verify_entries(folder, result["artifacts"])
        slots.append(slot)
        for period in ("evaluation", "observed_2026"):
            name = f"scores_{period}.parquet"
            if name in result["artifacts"]:
                files.append(
                    {
                        "slot": slot,
                        "model": result["summary"]["kind"],
                        "period": period,
                        "path": (folder / name).relative_to(root).as_posix(),
                        **result["artifacts"][name],
                    }
                )
    if slots != plan["slots"] or len(files) != 8:
        raise DataValidationError("expected six fits and eight evaluation-period files")
    return {
        "scores": files,
        "metadata": metadata,
        "historical_code": historical,
        "plan": plan,
        "report": report,
    }


def prepare_manifest(root):
    config = json.loads((root / CONFIG).read_text())
    rolling = verify_rolling(root, config)
    rules = rule_report(root)
    if rules["fingerprint"] != config["rule_report"]:
        raise DataValidationError("rule evidence identity changed")
    _, sessions, groups = source_paths(root, config)
    paths = [p for items in groups.values() for p in items]
    paths += [s["path"] for s in rolling["scores"]]
    paths += [ROLLING + "/" + p for p in rolling["metadata"]["artifacts"]]
    paths += [ROLLING + "/" + n for n in ("plan.json", "report.json", "metadata.json")]
    paths += [
        config["cost_profile"],
        "config/research_signal_feasibility_v1.json",
        HISTORY + "/inventory.json",
        "config/historical_rule_catalogue_v2.json",
        "config/historical_rule_sources_v2.json",
        "data/products/rule_evidence/rule_catalogue_v2_20260911/report.json",
    ]
    journal = root / OUTPUT / "source_downloads.jsonl"
    downloads = [json.loads(line) for line in journal.read_text().splitlines()]
    documents = {d["id"]: d for d in downloads if d.get("status") == 200}
    if set(documents) != {
        "tushare_dividend",
        "stamp_law",
        "stamp_half",
        "stamp_2008",
        "dividend_2012",
        "dividend_2015",
    }:
        raise DataValidationError("missing reviewed public source")
    for document in documents.values():
        source = root / document["path"]
        if _sha(source) != document["sha256"] or source.stat().st_size != document["bytes"]:
            raise DataValidationError("downloaded public source changed before inventory pinning")
    if (
        sum(d.get("bytes", d.get("known_received_bytes", 0)) for d in downloads)
        > config["resources"]["max_source_bytes"]
    ):
        raise DataValidationError("public source budget exceeded")
    paths += [d["path"] for d in documents.values()] + [journal.relative_to(root).as_posix()]
    paths = sorted(set(paths))
    files = {
        p: {"sha256": _sha(root / p), "bytes": (root / p).stat().st_size}
        for p in paths
        if (root / p).is_file()
    }
    return {
        "contract_sha256": _sha(root / CONFIG),
        "sessions": sessions,
        "groups": groups,
        "scores": rolling["scores"],
        "files": files,
        "missing": [p for p in paths if p not in files],
        "documents": documents,
        "downloads": downloads,
        "historical_code": rolling["historical_code"],
    }


def load_inputs(root):
    manifest = json.loads((root / MANIFEST).read_text())
    if _sha(root / CONFIG) != manifest["contract_sha256"]:
        raise DataValidationError("pinned cost audit contract changed")
    config = json.loads((root / CONFIG).read_text())
    verify_entries(root, manifest["files"])
    if any((root / p).exists() for p in manifest["missing"]):
        raise DataValidationError("previously absent input appeared")
    inventory, sessions, groups = source_paths(root, config)
    if groups != manifest["groups"] or sessions != manifest["sessions"]:
        raise DataValidationError("input snapshot membership changed")
    rolling = verify_rolling(root, config)
    if rolling["scores"] != manifest["scores"]:
        raise DataValidationError("saved score bindings changed")
    if rule_report(root)["fingerprint"] != config["rule_report"]:
        raise DataValidationError("rule evidence changed")
    return config, manifest, inventory, rolling
