"""Earlier reviewed intervals; delegate all existing dates to the frozen v2."""

from __future__ import annotations

import copy
import json
from dataclasses import asdict
from datetime import date

from quantlab.data.models import DataValidationError, canonical_payload_fingerprint
from quantlab.research.input_audit import _sha
from quantlab.research.round2_dataset import sealed_read, verify_entries
from quantlab.research.rule_evidence import EvidenceCatalogue
from quantlab.research.rule_evidence_v2 import load_catalogue as load_v2

CONFIG = "config/historical_rule_catalogue_v3.json"
MANIFEST = "config/historical_rule_sources_v3.json"
OUTPUT = "data/products/rule_evidence/rule_catalogue_v3_20260913"
PARENT_REPORT_PATH = "data/products/rule_evidence/rule_catalogue_v2_20260911/report.json"
PARENT_REPORT = "0875df1c23e547c209658fd8c9440af6f3b0e22feeb487231dd80b73c0cbba28"
BOUNDARIES = {
    "SSE": ("2020-03-13", "2022-09-04", "2022-09-05"),
    "SZSE": ("2021-04-06", "2022-08-21", "2022-08-22"),
}


class EarlierCatalogue:
    def __init__(self, config, parent):
        if canonical_payload_fingerprint(parent.contract) != config["parent_contract_fingerprint"]:
            raise DataValidationError("v3 parent contract changed")
        if (
            config["version"] != "historical-rule-evidence-v3"
            or config["execution_authority"] is not False
            or config["audit_interval"] != ["2020-01-01", "2026-09-10"]
            or config["calendar"] != {**parent.contract["calendar"], "expected_sessions": 1623}
            or not config["additional_limitations"]
        ):
            raise DataValidationError("v3 changed its fixed audit scope")
        specs = config["additions"]
        if len(specs) != 8 or len({s["id"] for s in specs}) != 8:
            raise DataValidationError("v3 requires exactly eight unique earlier intervals")
        old = {r["id"]: r for r in parent.contract["intervals"] if "source_chain" in r}
        expected = {}
        for identity, r in old.items():
            base, before, amendment = BOUNDARIES[r["exchange"]]
            for tag, start, end in (("base", base, before), ("amended", amendment, "2022-12-31")):
                key = identity.replace("pre2023-evidence-v2", f"{tag}-pre2023-evidence-v3")
                expected[key] = {
                    "id": key,
                    "parent_interval_id": identity,
                    "from": start,
                    "through": end,
                    "legal_effective_from": start,
                    "superseded_on": amendment if tag == "base" else "2023-04-10",
                }
        if {s["id"]: s for s in specs} != expected:
            raise DataValidationError("v3 additions differ from frozen dates or parent scopes")
        additions = []
        for spec in specs:
            r = copy.deepcopy(old[spec["parent_interval_id"]])
            r.update({k: v for k, v in spec.items() if k != "parent_interval_id"})
            removed = {
                e["source_id"] for e in r["source_chain"] if e["effective_from"] > spec["from"]
            }
            r["source_chain"] = [e for e in r["source_chain"] if e["source_id"] not in removed]
            r["source_ids"] = [s for s in r["source_ids"] if s not in removed]
            r["effective_evidence"] = [
                e for e in r["effective_evidence"] if not set(e["source_ids"]) & removed
            ]
            if removed:
                r["edition"] = r["edition"].removesuffix("+2022")
                r["supersession_evidence"] = {
                    "source_ids": sorted(removed),
                    "clause": "2022大宗修订开始形成新规则组合；不表示本子集普通数量条款改变。",
                }
            if max(e["effective_from"] for e in r["source_chain"]) != r["legal_effective_from"]:
                raise DataValidationError("earlier composite date lacks operative source support")
            for e in r["source_chain"]:
                document = parent.manifest["documents"][e["source_id"]]
                if not document["published_on"] <= e["effective_from"] <= r["from"]:
                    raise DataValidationError("earlier source chain has a future operative event")
            additions.append(r)
        self.parent, self.manifest = parent, parent.manifest
        self.contract = {
            **parent.contract,
            **config,
            "intervals": [*copy.deepcopy(parent.contract["intervals"]), *additions],
            "limitations": [
                *[
                    s.replace(
                        "本目录仍仅返回2023起的审计范围", "冻结v2仅返回2023起范围，v3范围另列"
                    )
                    for s in parent.contract["limitations"]
                ],
                *config["additional_limitations"],
            ],
        }
        self.start, self.end = map(date.fromisoformat, config["audit_interval"])
        self.earlier = EvidenceCatalogue({**self.contract, "intervals": additions}, self.manifest)
        self.records = {**parent.records, **self.earlier.records}

    def resolve(self, exchange, board, day, **kwargs):
        # Preserve v2 version, interval bounds, source tuples and limitations too.
        book = self.parent if day >= date(2023, 1, 1) else self.earlier
        return book.resolve(exchange, board, day, **kwargs)


def load_catalogue(root):
    manifest = json.loads((root / MANIFEST).read_text())
    if _sha(root / CONFIG) != manifest["contract_sha256"]:
        raise DataValidationError("pinned v3 contract changed")
    verify_entries(root, manifest["files"])
    for name, entry in manifest["files"].items():
        if (root / name).stat().st_size != entry["bytes"]:
            raise DataValidationError("v3 source byte count changed")
    parent = load_v2(root)  # Revalidates every inherited original source byte binding.
    return EarlierCatalogue(json.loads((root / CONFIG).read_text()), parent)


def compare_to_v2(result, parent_report, catalogue):
    if parent_report["fingerprint"] != PARENT_REPORT:
        raise DataValidationError("wrong saved v2 report")
    key = lambda r: (r["exchange"], r["board"], r["session"])  # noqa: E731
    old = {key(r): r for r in parent_report["rows"]}
    rows = result["rows"]
    current = {key(r): r for r in rows}
    if len(current) != len(rows) or len(old) != len(parent_report["rows"]):
        raise DataValidationError("duplicate rule audit identity")
    inherited = {k: v for k, v in current.items() if k[2] >= "2023-01-01"}
    if inherited != old:
        raise DataValidationError("v3 changed or omitted a saved v2 audit row")
    for exchange, board, day in old:
        when = date.fromisoformat(day)
        before = catalogue.parent.resolve(exchange, board, when)
        after = catalogue.resolve(exchange, board, when)
        if asdict(before) != asdict(after):
            raise DataValidationError("v3 changed a complete inherited resolver payload")
    earlier = [r for r in rows if r["session"] < "2023-01-01"]
    return {
        "parent_fingerprint": PARENT_REPORT,
        "unchanged_rows": len(old),
        "unchanged_resolver_payloads": len(old),
        "earlier_rows": len(earlier),
        "earlier_covered": sum(r["catalogue_rule"] is not None for r in earlier),
        "earlier_unknown": sum(r["catalogue_rule"] is None for r in earlier),
        "covered_2022": sum(
            r["catalogue_rule"] is not None and r["session"][:4] == "2022" for r in earlier
        ),
    }


def read_report(root):
    report = sealed_read(root / OUTPUT / "report.json")
    catalogue = load_catalogue(root)
    if (
        report["catalogue_sha256"] != _sha(root / CONFIG)
        or report["source_manifest_sha256"] != _sha(root / MANIFEST)
        or report["execution_authority"] is not False
    ):
        raise DataValidationError("v3 report scope or authority changed")
    if (
        compare_to_v2(report, sealed_read(root / PARENT_REPORT_PATH), catalogue)
        != report["comparison"]
    ):
        raise DataValidationError("v3 saved comparison does not reconcile")
    return report
