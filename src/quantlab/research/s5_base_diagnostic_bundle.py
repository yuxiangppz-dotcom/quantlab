"""Content-addressed in-memory delivery bundle for the S5-B diagnostic."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any

from quantlab.data.models import canonical_payload_fingerprint
from quantlab.research.s5_base_diagnostic_metrics import S5BaseDiagnosticMetrics
from quantlab.research.s5_base_diagnostic_protocol import (
    frozen_s5_base_diagnostic_protocol,
)
from quantlab.research.s5_base_diagnostic_review import (
    S5BaseDiagnosticReviewArtifact,
    build_s5_base_diagnostic_review,
)
from quantlab.research.s5_base_diagnostic_run_seal import S5BaseDiagnosticRunSeal

_SCHEMA = "quantlab_s5b_diagnostic_bundle_v1"
_FILES = (
    ("s5b_metrics.json", "application/json"),
    ("s5b_run_seal.json", "application/json"),
    ("s5b_review.md", "text/markdown; charset=utf-8"),
)


@dataclass(frozen=True)
class S5BaseDiagnosticBundleFile:
    name: str
    media_type: str
    content: str
    byte_length: int
    sha256: str

    def __post_init__(self) -> None:
        if not self.name or not self.media_type or not self.content:
            raise ValueError("bundle file identity and content must be non-empty")
        encoded = self.content.encode("utf-8")
        if self.byte_length != len(encoded):
            raise ValueError("bundle file byte_length does not match UTF-8 content")
        if self.sha256 != hashlib.sha256(encoded).hexdigest():
            raise ValueError("bundle file SHA-256 does not match content")


@dataclass(frozen=True)
class S5BaseDiagnosticBundle:
    schema: str
    strategy_id: str
    protocol_fingerprint: str
    input_fingerprint: str
    metrics_fingerprint: str
    seal_fingerprint: str
    review_fingerprint: str
    review_content_fingerprint: str
    files: tuple[S5BaseDiagnosticBundleFile, ...]
    diagnostic_only: bool = True
    executable_pnl: bool = False
    holding_policy_frozen: bool = False
    performance_verdict: bool = False
    promotion_authority: bool = False
    account_mutation_authority: bool = False
    broker_order_authority: bool = False
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        protocol = frozen_s5_base_diagnostic_protocol()
        if self.schema != _SCHEMA:
            raise ValueError(f"schema must equal {_SCHEMA}")
        if self.strategy_id != protocol.strategy_id:
            raise ValueError("strategy_id must equal the frozen S5-B strategy")
        if self.protocol_fingerprint != protocol.fingerprint:
            raise ValueError("protocol_fingerprint must equal the frozen protocol")
        for name in (
            "input_fingerprint",
            "metrics_fingerprint",
            "seal_fingerprint",
            "review_fingerprint",
            "review_content_fingerprint",
        ):
            value = getattr(self, name)
            if not value or not value.strip():
                raise ValueError(f"{name} must be non-empty")
        identities = tuple((item.name, item.media_type) for item in self.files)
        if identities != _FILES:
            raise ValueError("bundle files must equal the frozen ordered file contract")
        if (
            not self.diagnostic_only
            or self.executable_pnl
            or self.holding_policy_frozen
            or self.performance_verdict
            or self.promotion_authority
            or self.account_mutation_authority
            or self.broker_order_authority
        ):
            raise ValueError("bundle cannot acquire performance or execution authority")
        object.__setattr__(
            self,
            "fingerprint",
            canonical_payload_fingerprint(_bundle_payload(self)),
        )


def build_s5_base_diagnostic_bundle(
    *,
    metrics: S5BaseDiagnosticMetrics,
    seal: S5BaseDiagnosticRunSeal,
    review: S5BaseDiagnosticReviewArtifact,
) -> S5BaseDiagnosticBundle:
    """Build deterministic UTF-8 deliverables without touching a filesystem."""

    expected_review = build_s5_base_diagnostic_review(metrics=metrics, seal=seal)
    if review != expected_review or review.fingerprint != expected_review.fingerprint:
        raise ValueError("review does not match an independent deterministic rebuild")

    artifacts = (
        _file("s5b_metrics.json", "application/json", _canonical_json(metrics)),
        _file("s5b_run_seal.json", "application/json", _canonical_json(seal)),
        _file(
            "s5b_review.md",
            "text/markdown; charset=utf-8",
            _one_trailing_newline(review.markdown_zh),
        ),
    )
    bundle = S5BaseDiagnosticBundle(
        schema=_SCHEMA,
        strategy_id=review.strategy_id,
        protocol_fingerprint=review.protocol_fingerprint,
        input_fingerprint=review.input_fingerprint,
        metrics_fingerprint=review.metrics_fingerprint,
        seal_fingerprint=review.seal_fingerprint,
        review_fingerprint=review.fingerprint,
        review_content_fingerprint=review.content_fingerprint,
        files=artifacts,
    )
    verify_s5_base_diagnostic_bundle(bundle)
    return bundle


def verify_s5_base_diagnostic_bundle(
    bundle: S5BaseDiagnosticBundle,
) -> None:
    """Fail closed when any in-memory file or embedded identity has drifted."""

    expected_bundle_fingerprint = canonical_payload_fingerprint(
        _bundle_payload(bundle)
    )
    if bundle.fingerprint != expected_bundle_fingerprint:
        raise ValueError("bundle fingerprint does not match its manifest")
    for item, expected in zip(bundle.files, _FILES, strict=True):
        if (item.name, item.media_type) != expected:
            raise ValueError("bundle file identity changed")
        encoded = item.content.encode("utf-8")
        if item.byte_length != len(encoded):
            raise ValueError(f"{item.name} byte length changed")
        if item.sha256 != hashlib.sha256(encoded).hexdigest():
            raise ValueError(f"{item.name} SHA-256 changed")

    metrics_payload = _parse_json_file(bundle.files[0])
    seal_payload = _parse_json_file(bundle.files[1])
    if metrics_payload.get("fingerprint") != bundle.metrics_fingerprint:
        raise ValueError("metrics JSON fingerprint does not match bundle")
    if metrics_payload.get("input_fingerprint") != bundle.input_fingerprint:
        raise ValueError("metrics JSON input fingerprint does not match bundle")
    if metrics_payload.get("protocol_fingerprint") != bundle.protocol_fingerprint:
        raise ValueError("metrics JSON protocol fingerprint does not match bundle")
    if seal_payload.get("fingerprint") != bundle.seal_fingerprint:
        raise ValueError("seal JSON fingerprint does not match bundle")
    if seal_payload.get("metrics_fingerprint") != bundle.metrics_fingerprint:
        raise ValueError("seal JSON metrics fingerprint does not match bundle")
    if seal_payload.get("input_fingerprint") != bundle.input_fingerprint:
        raise ValueError("seal JSON input fingerprint does not match bundle")
    if seal_payload.get("protocol_fingerprint") != bundle.protocol_fingerprint:
        raise ValueError("seal JSON protocol fingerprint does not match bundle")

    review_content = bundle.files[2].content
    if not review_content.endswith("\n") or review_content.endswith("\n\n"):
        raise ValueError("review Markdown must have exactly one trailing newline")
    expected_content_fingerprint = canonical_payload_fingerprint(
        {"markdown_zh": review_content}
    )
    if expected_content_fingerprint != bundle.review_content_fingerprint:
        raise ValueError("review Markdown fingerprint does not match bundle")


def _file(name: str, media_type: str, content: str) -> S5BaseDiagnosticBundleFile:
    encoded = content.encode("utf-8")
    return S5BaseDiagnosticBundleFile(
        name=name,
        media_type=media_type,
        content=content,
        byte_length=len(encoded),
        sha256=hashlib.sha256(encoded).hexdigest(),
    )


def _canonical_json(value: object) -> str:
    return (
        json.dumps(
            _jsonable(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    )


def _jsonable(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _jsonable(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise TypeError(f"unsupported diagnostic bundle value: {type(value).__name__}")


def _parse_json_file(item: S5BaseDiagnosticBundleFile) -> dict[str, object]:
    if not item.content.endswith("\n") or item.content.endswith("\n\n"):
        raise ValueError(f"{item.name} must have exactly one trailing newline")
    try:
        payload = json.loads(item.content)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{item.name} is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{item.name} JSON root must be an object")
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ) + "\n"
    if canonical != item.content:
        raise ValueError(f"{item.name} is not canonical JSON")
    return payload


def _one_trailing_newline(value: str) -> str:
    return value.rstrip("\n") + "\n"


def _bundle_payload(bundle: S5BaseDiagnosticBundle) -> dict[str, object]:
    return {
        "schema": bundle.schema,
        "strategy_id": bundle.strategy_id,
        "protocol_fingerprint": bundle.protocol_fingerprint,
        "input_fingerprint": bundle.input_fingerprint,
        "metrics_fingerprint": bundle.metrics_fingerprint,
        "seal_fingerprint": bundle.seal_fingerprint,
        "review_fingerprint": bundle.review_fingerprint,
        "review_content_fingerprint": bundle.review_content_fingerprint,
        "files": [
            {
                "name": item.name,
                "media_type": item.media_type,
                "byte_length": item.byte_length,
                "sha256": item.sha256,
            }
            for item in bundle.files
        ],
        "diagnostic_only": bundle.diagnostic_only,
        "executable_pnl": bundle.executable_pnl,
        "holding_policy_frozen": bundle.holding_policy_frozen,
        "performance_verdict": bundle.performance_verdict,
        "promotion_authority": bundle.promotion_authority,
        "account_mutation_authority": bundle.account_mutation_authority,
        "broker_order_authority": bundle.broker_order_authority,
    }
