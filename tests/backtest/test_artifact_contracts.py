"""Domain-semantic validators are bound BEFORE the formal commit point.

A contract's semantic validators receive the run directory and run in all
three enforcement points: the publisher's preflight (before promotion), the
post-promotion formal verification (before the marker is accepted), and the
independent external verifier. A semantic failure can therefore never leave
behind a promotable staging directory, a valid completion marker, or a
final directory the formal verifier accepts.
"""

import json
from datetime import datetime

import pytest

from quantlab.artifacts import (
    COMPLETION_MARKER,
    ArtifactContract,
    ArtifactPublisher,
)
from quantlab.backtest.artifacts import (
    INCOMPLETE_MARKER,
    backtest_artifact_contract,
)

SCHEMA = "domain_semantic_v1"
HEAD = "2b050c205089f995ffdd00c988e701c15389b112"
RUN_ID = "20260105T000000"


def _flaky_validator(state: dict):
    def validator(contract, run_dir):
        if state.get("fail"):
            return ("injected domain semantic failure",)
        return ()

    return validator


def _contract(validator) -> ArtifactContract:
    return ArtifactContract(
        name="domain_semantic",
        schema=SCHEMA,
        groups={},
        top_level=("summary.json", "evidence.json"),
        semantic_validators=(validator,),
    )


def _fill_payload(staging, *, summary_claims_success: bool = False) -> None:
    (staging / "summary.json").write_text(json.dumps({
        "code_version": HEAD, "run_id": RUN_ID, "experiment_schema": SCHEMA,
        **({"claims": {"formal_run_valid": True}} if summary_claims_success else {}),
    }))
    (staging / "evidence.json").write_text(json.dumps({"ok": True}))


def _publisher(tmp_path, contract) -> ArtifactPublisher:
    return ArtifactPublisher(
        tmp_path, RUN_ID, expected_registry=contract, head=HEAD, schema=SCHEMA,
    )


def _staged(tmp_path, contract, **payload_kwargs) -> ArtifactPublisher:
    publisher = _publisher(tmp_path, contract)
    _fill_payload(publisher.staging, **payload_kwargs)
    return publisher


def test_semantic_failure_before_promotion_is_fail_closed(tmp_path) -> None:
    state = {"fail": True}
    publisher = _staged(tmp_path, _contract(_flaky_validator(state)))
    with pytest.raises(RuntimeError, match="semantic"):
        publisher.publish()
    assert (tmp_path / RUN_ID).exists() is False
    assert (publisher.staging / COMPLETION_MARKER).exists() is False
    assert (publisher.staging / INCOMPLETE_MARKER).exists()
    with pytest.raises(RuntimeError):
        from quantlab.artifacts import verify_formal_artifact

        verify_formal_artifact(publisher.staging, _contract(_flaky_validator(state)))


def test_semantic_failure_after_promotion_is_fail_closed(
    tmp_path, monkeypatch
) -> None:
    """The validator passes preflight, then fails after promotion: the final
    directory must lose its marker, gain an explicit INCOMPLETE record, and
    be rejected by the formal verifier."""
    import os

    state = {"fail": False}
    publisher = _staged(tmp_path, _contract(_flaky_validator(state)))
    real_replace = os.replace

    def replace_then_flip(source, target):
        result = real_replace(source, target)
        if str(target) == str(publisher.final):
            state["fail"] = True  # flip only at the promotion boundary
        return result

    monkeypatch.setattr(
        "quantlab.backtest.artifacts.os.replace", replace_then_flip
    )
    with pytest.raises(RuntimeError, match="semantic"):
        publisher.publish()
    monkeypatch.setattr("quantlab.backtest.artifacts.os.replace", real_replace)
    final = tmp_path / RUN_ID
    assert final.exists()
    assert (final / COMPLETION_MARKER).exists() is False
    assert (final / INCOMPLETE_MARKER).exists()
    with pytest.raises(RuntimeError, match="semantic"):
        from quantlab.artifacts import verify_formal_artifact

        verify_formal_artifact(final, _contract(_flaky_validator(state)))


def test_semantic_validator_keyboard_interrupt_is_fail_closed(tmp_path) -> None:
    def interrupting_validator(contract, run_dir):
        raise KeyboardInterrupt("domain validator killed")

    publisher = _staged(tmp_path, _contract(interrupting_validator))
    with pytest.raises(KeyboardInterrupt):
        publisher.publish()
    assert (tmp_path / RUN_ID).exists() is False
    assert (publisher.staging / COMPLETION_MARKER).exists() is False
    assert (publisher.staging / INCOMPLETE_MARKER).exists()


def test_success_documents_cannot_overrule_semantic_failure(tmp_path) -> None:
    """marker/manifest/summary all claiming success cannot overrule a domain
    semantic failure: the external formal verifier must reject."""

    def always_failing(contract, run_dir):
        return ("domain semantics never satisfied",)

    publisher = _staged(
        tmp_path, _contract(always_failing), summary_claims_success=True
    )
    with pytest.raises(RuntimeError, match="semantic"):
        publisher.publish()


def test_backtest_contract_remains_backward_compatible(tmp_path) -> None:
    """The v0.1.3 research-backtest contract keeps working after the
    validator signature gained the run directory argument."""
    from quantlab.artifacts import verify_formal_artifact

    contract = backtest_artifact_contract(
        schema=SCHEMA,
        groups={
            "primary_strategy_recovery_assumption_1": ["records.csv"],
            "primary_strategy_recovery_assumption_0": ["records.csv"],
            "equal_weight_v1_control_recovery_assumption_1": ["records.csv"],
            "equal_weight_v1_control_recovery_assumption_0": ["records.csv"],
        },
        top_level=("summary.json",),
    )
    publisher = _publisher(tmp_path, contract)
    stamp = datetime(2026, 1, 5).isoformat()
    for group in contract.groups:
        (publisher.staging / f"{group}_records.csv").write_text(
            "trade_date,nav\n" + stamp + ",1.0\n"
        )
    (publisher.staging / "summary.json").write_text(json.dumps({
        "code_version": HEAD, "run_id": RUN_ID, "experiment_schema": SCHEMA,
    }))
    final = publisher.publish()
    result = verify_formal_artifact(final, contract, expected_head=HEAD)
    assert result["complete"] is True
