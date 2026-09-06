"""Domain separation for the reusable formal artifact contract."""

import json

import pytest

from quantlab.artifacts import (
    ArtifactContract,
    ArtifactPublisher,
    verify_formal_artifact,
)
from quantlab.backtest.artifacts import backtest_artifact_contract

SCHEMA = "execution_readiness_v0_1"
BACKTEST_SCHEMA = "performance_baseline_benchmark_correctness_v0_1_3"
HEAD = "2b050c205089f995ffdd00c988e701c15389b112"
RUN_ID = "20260105T000000"


def test_generic_contract_does_not_require_backtest_groups(tmp_path) -> None:
    """The fail-closed protocol is reusable by non-backtest artifacts."""
    contract = ArtifactContract(
        name="execution_readiness",
        schema=SCHEMA,
        groups={},
        top_level=("summary.json",),
    )
    publisher = ArtifactPublisher(
        tmp_path,
        RUN_ID,
        expected_registry=contract,
        head=HEAD,
        schema=SCHEMA,
    )
    summary = {
        "code_version": HEAD,
        "experiment_schema": SCHEMA,
        "run_id": RUN_ID,
    }
    (publisher.staging / "summary.json").write_text(json.dumps(summary))

    final = publisher.publish(summary)

    result = verify_formal_artifact(
        final,
        contract,
        expected_run_id=RUN_ID,
        expected_head=HEAD,
        expected_schema=SCHEMA,
    )
    assert result["complete"] is True
    assert result["verified_files"] == 1


def test_backtest_contract_requires_both_primary_recovery_bounds(tmp_path) -> None:
    groups = {
        "primary_strategy_recovery_assumption_1": ["report.json"],
        "primary_strategy_recovery_assumption_0": ["report.json"],
        "equal_weight_v1_control_recovery_assumption_1": ["report.json"],
    }
    contract = backtest_artifact_contract(
        schema=BACKTEST_SCHEMA,
        groups=groups,
        top_level=["summary.json"],
    )

    with pytest.raises(
        RuntimeError,
        match="registry missing equal_weight_v1_control_recovery_assumption_0",
    ):
        verify_formal_artifact(
            tmp_path / RUN_ID,
            contract,
            expected_run_id=RUN_ID,
            expected_head=HEAD,
            expected_schema=BACKTEST_SCHEMA,
        )
