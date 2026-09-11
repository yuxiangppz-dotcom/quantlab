"""Separately versioned, read-only exchange-rule evidence; no execution authority.

The catalogue is a retrospective document review. Retrieval timestamps never imply
that this project possessed those bytes at the historical applicability date.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from quantlab.data.models import DataValidationError
from quantlab.execution.models import OrderSession, OrderType
from quantlab.execution.rules import AShareTradingRule, PITRuleBook, RuleSource
from quantlab.research.input_audit import _sha
from quantlab.research.round2_dataset import sealed_read, verify_entries

OUTPUT = "data/products/rule_evidence/rule_catalogue_20260911"
CONFIG = "config/historical_rule_catalogue_v1.json"
MANIFEST = "config/historical_rule_sources_v1.json"
SCOPES = (("SSE", "MAIN"), ("SSE", "STAR"), ("SZSE", "MAIN"), ("SZSE", "CHINEXT"))
FIELDS = (
    "price_tick", "buy_min_quantity", "buy_quantity_step", "sell_min_quantity",
    "sell_quantity_step", "max_limit_quantity", "allow_full_odd_lot_exit",
    "sellability_lag_sessions",
)


def values(rule):
    return {name: str(value) if isinstance(value, Decimal) else value
            for name in FIELDS for value in (getattr(rule, name),)}


class EvidenceCatalogue:
    """Only return the reviewed continuous-auction limit-order subset.

    This intentionally is not a PITRuleBook subclass and has no promotion API.
    Stock identity, account eligibility and price-band/cage checks remain separate.
    """

    def __init__(self, contract: dict, manifest: dict):
        self.contract, self.manifest = contract, manifest
        if contract["execution_authority"] is not False:
            raise DataValidationError("research catalogue cannot grant execution authority")
        self.start, self.end = map(date.fromisoformat, contract["audit_interval"])
        if self.start > self.end or not contract["limitations"]:
            raise DataValidationError("invalid catalogue coverage or limitations")
        documents = manifest["documents"]
        sources = {}
        for key, document in documents.items():
            retrieved = datetime.fromisoformat(document["retrieved_at"])
            if retrieved.tzinfo is None:
                raise DataValidationError("source retrieval time must be timezone aware")
            sources[key] = RuleSource(
                authority=document["authority"], title=document["title"],
                url=document["url"], published_on=date.fromisoformat(document["published_on"]),
                retrieved_on=retrieved.date(), document_sha256=document["sha256"],
            )
        rules = []
        self.records = {}
        for entry in contract["intervals"]:
            start, end = map(date.fromisoformat, (entry["from"], entry["through"]))
            legal_start = date.fromisoformat(entry["legal_effective_from"])
            legal_end = entry["superseded_on"]
            if (entry["exchange"], entry["board"]) not in SCOPES:
                raise DataValidationError("unsupported common-stock scope")
            if not self.start <= start <= end <= self.end or start < legal_start:
                raise DataValidationError("catalogue interval outside reviewed legal scope")
            if legal_end and end >= date.fromisoformat(legal_end):
                raise DataValidationError("rule interval extends past supersession")
            if set(entry["values"]) != set(FIELDS) or set(entry["provisions"]) != set(FIELDS):
                raise DataValidationError("unsupported or missing rule field")
            if not entry["effective_evidence"] or not entry["source_ids"]:
                raise DataValidationError("missing effective-date evidence")
            for evidence in [*entry["effective_evidence"], *entry["provisions"].values()]:
                if not evidence or not evidence["clause"] or not evidence["source_ids"]:
                    raise DataValidationError("missing source provision")
                if not set(evidence["source_ids"]) <= set(entry["source_ids"]):
                    raise DataValidationError("provision refers to an unbound source")
            if any(sources[key].published_on > legal_start for key in entry["source_ids"]):
                raise DataValidationError("operative source published after rule effective date")
            if legal_end:
                supersession = entry["supersession_evidence"]
                if (not supersession or not supersession["clause"]
                        or not supersession["source_ids"]):
                    raise DataValidationError("missing supersession evidence")
                if any(sources[key].published_on > date.fromisoformat(legal_end)
                       for key in supersession["source_ids"]):
                    raise DataValidationError("supersession source postdates supersession")
            v = entry["values"]
            if type(v["allow_full_odd_lot_exit"]) is not bool:
                raise DataValidationError("invalid full odd-lot flag")
            for name in set(FIELDS) - {"price_tick", "allow_full_odd_lot_exit"}:
                if type(v[name]) is not int or v[name] < 1:
                    raise DataValidationError("rule quantity/lag must be a positive integer")
            if v["max_limit_quantity"] < max(v["buy_min_quantity"], v["sell_min_quantity"]):
                raise DataValidationError("maximum smaller than minimum order")
            rule = AShareTradingRule(
                rule_id=entry["id"], version=contract["version"] + ":" + entry["edition"],
                exchange=entry["exchange"], board=entry["board"],
                effective_from=start, effective_to=end,
                sources=tuple(sources[key] for key in entry["source_ids"]),
                supported_order_types=(OrderType.LIMIT,),
                supported_sessions=(OrderSession.CONTINUOUS_AUCTION,),
                **{**v, "price_tick": Decimal(v["price_tick"])},
                limitations=tuple(contract["limitations"]),
            )
            rules.append(rule)
            self.records[rule.rule_id] = entry
        self._book = PITRuleBook(tuple(rules))

    def resolve(self, exchange, board, day, *, order_type=OrderType.LIMIT,
                session=OrderSession.CONTINUOUS_AUCTION, fields=FIELDS):
        if (order_type != OrderType.LIMIT or session != OrderSession.CONTINUOUS_AUCTION
                or not set(fields) <= set(FIELDS)):
            return None
        return self._book.resolve(exchange, board, day)


def load_catalogue(root: Path):
    manifest = json.loads((root / MANIFEST).read_text())
    if _sha(root / CONFIG) != manifest["contract_sha256"]:
        raise DataValidationError("pinned catalogue contract changed")
    verify_entries(root, manifest["files"])
    for name, entry in manifest["files"].items():
        if (root / name).stat().st_size != entry["bytes"]:
            raise DataValidationError("source byte count mismatch")
    for document in manifest["documents"].values():
        entry = manifest["files"].get(document["path"])
        if entry != {"sha256": document["sha256"], "bytes": document["bytes"]}:
            raise DataValidationError("source document is not byte bound")
    contract = json.loads((root / CONFIG).read_text())
    saved = sum(d["bytes"] for d in manifest["documents"].values())
    if saved + manifest["failed_prefix_bytes_known"] > contract["resources"]["max_source_bytes"]:
        raise DataValidationError("downloaded source byte budget exceeded")
    return EvidenceCatalogue(contract, manifest)


def baseline_fingerprint(book):
    from quantlab.data.models import canonical_payload_fingerprint

    # Explicit stable serialization includes identities, provenance and limitations.
    return canonical_payload_fingerprint(json.loads(json.dumps(
        [asdict(rule) for rule in book.rules], default=str, sort_keys=True,
    )))


def audit_dates(catalogue, sessions, baseline):
    if not sessions or tuple(sorted(set(sessions))) != tuple(sessions):
        raise DataValidationError("audit sessions must be nonempty, unique and sorted")
    if any(not catalogue.start <= day <= catalogue.end for day in sessions):
        raise DataValidationError("audit date outside fixed interval")
    rows, summary, intervals = [], [], []
    for exchange, board in SCOPES:
        scoped = []
        for day in sessions:
            old = baseline.resolve(exchange, board, day)
            new = catalogue.resolve(exchange, board, day)
            differences = {field: {"baseline": values(old)[field], "catalogue": values(new)[field]}
                           for field in FIELDS if old and new
                           and values(old)[field] != values(new)[field]}
            row = {
                "exchange": exchange, "board": board, "session": day.isoformat(),
                "baseline_rule": old.rule_id if old else None,
                "catalogue_rule": new.rule_id if new else None,
                "status": ("conflict" if differences else "verified_existing" if old and new
                           else "newly_covered" if new else "still_unknown"),
                "baseline_values": values(old) if old else None,
                "catalogue_values": values(new) if new else None,
                "differences": differences,
                "unknown_reason": None if new else catalogue.contract["uncovered_reason"],
            }
            scoped.append(row)
            key = (row["status"], row["baseline_rule"], row["catalogue_rule"])
            if (intervals and (intervals[-1]["exchange"], intervals[-1]["board"]) ==
                    (exchange, board) and intervals[-1]["key"] == key):
                intervals[-1]["through"] = row["session"]
                intervals[-1]["sessions"] += 1
            else:
                intervals.append({"exchange": exchange, "board": board, "key": key,
                                  "from": row["session"], "through": row["session"],
                                  "sessions": 1, "status": row["status"],
                                  "catalogue_rule": row["catalogue_rule"]})
        rows.extend(scoped)
        summary.append({
            "exchange": exchange, "board": board, "sessions": len(sessions),
            "baseline_covered": sum(r["baseline_rule"] is not None for r in scoped),
            "catalogue_covered": sum(r["catalogue_rule"] is not None for r in scoped),
            **{status: sum(r["status"] == status for r in scoped) for status in
               ("verified_existing", "newly_covered", "still_unknown", "conflict")},
        })
    return {"summary": summary, "rows": rows,
            "intervals": [{k: v for k, v in row.items() if k != "key"} for row in intervals]}


def read_report(root):
    path = root / OUTPUT / "report.json"
    if not path.exists():
        return None
    report = sealed_read(path)
    catalogue = load_catalogue(root)
    if report["catalogue_sha256"] != _sha(root / CONFIG):
        raise DataValidationError("audit report belongs to a different catalogue")
    if report["source_manifest_sha256"] != _sha(root / MANIFEST):
        raise DataValidationError("audit report belongs to a different source inventory")
    verify_entries(root, report["inputs"])
    if report["execution_authority"] is not False:
        raise DataValidationError("invalid audit authority")
    if report["version"] != catalogue.contract["version"]:
        raise DataValidationError("audit catalogue version mismatch")
    return report
