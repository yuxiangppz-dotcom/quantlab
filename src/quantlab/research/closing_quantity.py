"""Closing LIMIT quantity evidence only; delegates every old query unchanged."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date

from quantlab.data.models import DataValidationError, canonical_payload_fingerprint
from quantlab.execution.models import OrderSession, OrderType
from quantlab.research.input_audit import _sha
from quantlab.research.round2_dataset import sealed_read, verify_entries
from quantlab.research.rule_evidence import FIELDS, SCOPES
from quantlab.research.rule_evidence_v3 import load_catalogue as load_parent

CONFIG = "config/closing_quantity_v1.json"
MANIFEST = "config/closing_quantity_sources_v1.json"
OUTPUT = "data/products/rule_evidence/closing_quantity_20260913"
START, END = date(2022, 1, 1), date(2024, 12, 31)
VERSION = "closing-quantity-evidence-v1"
LIMITATIONS = (
    "仅2022–2024普通A股收盘集合竞价LIMIT数量子集；继承原规则有效边界。",
    "并非完整申报许可；不证明实际开市、竞价成交、价格范围、容量或现金流。",
    "收盘价可能来自最后一笔前一分钟加权均价或前收盘价，不能反推竞价成交。",
    "ADV20的1%/5%可作为已预注册日线模拟假设，不能证明收盘可成交容量。",
    "无执行、收益证据或策略晋升权限；不涵盖盘后定价、市价或大宗交易。",
)


class ClosingQuantityCatalogue:
    execution_authority = False
    performance_evidence = False

    def __init__(self, contract, parent):
        if (
            contract["version"] != VERSION
            or contract["execution_authority"] is not False
            or contract["performance_evidence"] is not False
            or contract["audit_interval"] != [START.isoformat(), END.isoformat()]
            or contract["fields"] != list(FIELDS)
            or contract["limitations"] != list(LIMITATIONS)
            or contract["parent_contract_fingerprint"]
            != canonical_payload_fingerprint(parent.contract)
        ):
            raise DataValidationError("closing subset scope or parent changed")
        self.parent, self.contract = parent, contract
        self.start, self.end = START, END
        expected = {
            identity
            for identity, record in parent.records.items()
            if record["from"] <= END.isoformat() and record["through"] >= START.isoformat()
        }
        if set(contract["provisions"]) != expected:
            raise DataValidationError("closing subset interval population changed")
        for identity, refs in contract["provisions"].items():
            record = parent.records[identity]
            if not refs or any(
                r["source_id"] not in record["source_ids"] or not r["clause"].strip() for r in refs
            ):
                raise DataValidationError("closing clause lacks operative parent source")

    def resolve(
        self,
        exchange,
        board,
        day,
        *,
        order_type=OrderType.LIMIT,
        session=OrderSession.CONTINUOUS_AUCTION,
        fields=FIELDS,
    ):
        if session != OrderSession.CLOSING_AUCTION:
            return self.parent.resolve(
                exchange, board, day, order_type=order_type, session=session, fields=fields
            )
        if (
            order_type != OrderType.LIMIT
            or not set(fields) <= set(FIELDS)
            or not START <= day <= END
            or (exchange, board) not in SCOPES
        ):
            return None
        rule = self.parent.resolve(exchange, board, day, order_type=order_type, fields=fields)
        if rule is None:
            return None
        clauses = tuple(
            f"{r['source_id']}: {r['clause']}" for r in self.contract["provisions"][rule.rule_id]
        )
        # No numeric override, source replacement, or extension of effective bounds.
        return replace(
            rule,
            rule_id=rule.rule_id + ":closing-subset-v1",
            version=rule.version + ":" + VERSION,
            supported_sessions=(OrderSession.CLOSING_AUCTION,),
            limitations=(*rule.limitations, *LIMITATIONS, *clauses),
        )


def load_catalogue(root):
    manifest = json.loads((root / MANIFEST).read_text(encoding="utf-8"))
    if _sha(root / CONFIG) != manifest["contract_sha256"]:
        raise DataValidationError("closing contract bytes changed")
    verify_entries(root, manifest["files"])
    if any(
        (root / name).stat().st_size != entry["bytes"] for name, entry in manifest["files"].items()
    ):
        raise DataValidationError("closing source sizes changed")
    parent = load_parent(root)
    return ClosingQuantityCatalogue(json.loads((root / CONFIG).read_text(encoding="utf-8")), parent)


def fixed_sessions(root, catalogue):
    spec = catalogue.contract["calendar"]
    inventory = sealed_read(root / spec["path"])
    days = tuple(
        date.fromisoformat(d)
        for d in inventory["sessions"]
        if START.isoformat() <= d <= END.isoformat()
    )
    if (
        inventory["fingerprint"] != spec["fingerprint"]
        or len(days) != 726
        or tuple(sorted(set(days))) != days
    ):
        raise DataValidationError("closing comparison calendar changed")
    return days
