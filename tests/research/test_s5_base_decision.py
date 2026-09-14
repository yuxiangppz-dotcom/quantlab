from dataclasses import replace
from datetime import date

import pytest

from quantlab.research.s5_base_completion import S5BaseObservation, S5BaseState
from quantlab.research.s5_base_decision import (
    S5BaseAdmissionState,
    S5BaseCandidate,
    S5BaseDecisionConfig,
    build_s5_base_decision,
)
from quantlab.research.s5_membership_audit import (
    S5MembershipAuditRow,
    S5MembershipAuditStatus,
)

AS_OF = date(2026, 9, 14)


def _observation(entity_id: str, **changes: object) -> S5BaseObservation:
    item = S5BaseObservation(
        entity_id=entity_id,
        as_of=AS_OF,
        prior_drawdown_from_120d_high=-0.30,
        recent_return_10=-0.01,
        max_drawdown_10=0.04,
        new_low_count_10=0,
        new_low_count_20=0,
        max_new_low_undercut_10=0.0,
        volatility_ratio_10_to_prior_20=0.8,
        recovery_from_10d_low=0.06,
        prior_low_reclaimed=True,
        reclaim_sessions=None,
        close_location_20=0.65,
        close_to_ma20_ratio=1.01,
        ma20_slope_5=0.002,
        breakout_distance_20=-0.01,
        volume_ratio_5_to_20=1.0,
        history_complete=True,
    )
    return replace(item, **changes)


def _membership(
    instrument_id: str,
    sector_id: str = "BANK",
    status: S5MembershipAuditStatus = S5MembershipAuditStatus.COVERED_VERIFIED,
) -> S5MembershipAuditRow:
    return S5MembershipAuditRow(
        instrument_id=instrument_id,
        as_of=AS_OF,
        expected_sector_id=sector_id,
        resolved_sector_id=sector_id,
        status=status,
        source_ids=("pit-fixture",),
        active_evidence_count=1,
    )


def _candidate(
    instrument_id: str,
    *,
    sector_id: str = "BANK",
    observation: S5BaseObservation | None = None,
    membership: S5MembershipAuditRow | None = None,
    research_eligible: bool | None = True,
) -> S5BaseCandidate:
    return S5BaseCandidate(
        instrument_id=instrument_id,
        sector_id=sector_id,
        observation=observation or _observation(instrument_id),
        membership=membership
        if membership is not None
        else _membership(instrument_id, sector_id),
        research_eligible=research_eligible,
    )


def test_ready_sector_and_stock_create_target_with_residual_cash() -> None:
    decision = build_s5_base_decision(
        as_of=AS_OF,
        sectors=(_observation("BANK"),),
        candidates=(_candidate("000001.SZ"),),
    )
    assert decision.selected_ids == ("000001.SZ",)
    assert decision.target.cash_weight == pytest.approx(0.96)
    assert decision.candidate_results[0].state is S5BaseAdmissionState.ELIGIBLE
    assert decision.broker_order_authority is False


def test_no_candidates_is_valid_all_cash() -> None:
    decision = build_s5_base_decision(as_of=AS_OF, sectors=(), candidates=())
    assert decision.selected_ids == ()
    assert decision.target.positions == ()
    assert decision.target.cash_weight == 1.0


@pytest.mark.parametrize(
    ("sector_changes", "reason"),
    [
        ({"recent_return_10": None}, "sector_state_unknown"),
        ({"recovery_from_10d_low": 0.01}, "sector_base_building"),
        ({"max_new_low_undercut_10": 0.10}, "sector_failed"),
    ],
)
def test_sector_gate_blocks_candidate(
    sector_changes: dict[str, object], reason: str
) -> None:
    sector = _observation("BANK", **sector_changes)
    if (
        "recent_return_10" in sector_changes
        and sector_changes["recent_return_10"] is None
    ):
        sector = replace(sector, history_complete=False)
    decision = build_s5_base_decision(
        as_of=AS_OF, sectors=(sector,), candidates=(_candidate("000001.SZ"),)
    )
    result = decision.candidate_results[0]
    assert result.selected is False
    assert result.reasons == (reason,)


def test_missing_sector_is_unknown() -> None:
    decision = build_s5_base_decision(
        as_of=AS_OF, sectors=(), candidates=(_candidate("000001.SZ"),)
    )
    assert decision.candidate_results[0].state is S5BaseAdmissionState.UNKNOWN


def test_missing_membership_is_unknown() -> None:
    candidate = replace(_candidate("000001.SZ"), membership=None)
    result = build_s5_base_decision(
        as_of=AS_OF,
        sectors=(_observation("BANK"),),
        candidates=(candidate,),
    ).candidate_results[0]
    assert result.state is S5BaseAdmissionState.UNKNOWN
    assert result.reasons == ("membership_missing",)


@pytest.mark.parametrize(
    "status",
    [
        S5MembershipAuditStatus.MISSING,
        S5MembershipAuditStatus.UNVERIFIED,
        S5MembershipAuditStatus.CONFLICTING,
    ],
)
def test_unresolved_membership_is_unknown(status: S5MembershipAuditStatus) -> None:
    membership = _membership("000001.SZ", status=status)
    decision = build_s5_base_decision(
        as_of=AS_OF,
        sectors=(_observation("BANK"),),
        candidates=(_candidate("000001.SZ", membership=membership),),
    )
    assert decision.candidate_results[0].state is S5BaseAdmissionState.UNKNOWN


def test_membership_mismatch_is_ineligible() -> None:
    membership = replace(
        _membership("000001.SZ"),
        status=S5MembershipAuditStatus.MISMATCH,
        resolved_sector_id="TECH",
    )
    result = build_s5_base_decision(
        as_of=AS_OF,
        sectors=(_observation("BANK"),),
        candidates=(_candidate("000001.SZ", membership=membership),),
    ).candidate_results[0]
    assert result.state is S5BaseAdmissionState.INELIGIBLE
    assert result.reasons == ("membership_sector_mismatch",)


def test_internally_inconsistent_covered_membership_is_ineligible() -> None:
    membership = replace(_membership("000001.SZ"), expected_sector_id="TECH")
    result = build_s5_base_decision(
        as_of=AS_OF,
        sectors=(_observation("BANK"),),
        candidates=(_candidate("000001.SZ", membership=membership),),
    ).candidate_results[0]
    assert result.state is S5BaseAdmissionState.INELIGIBLE
    assert result.reasons == ("membership_sector_mismatch",)


@pytest.mark.parametrize(
    ("eligible", "expected"),
    [(None, S5BaseAdmissionState.UNKNOWN), (False, S5BaseAdmissionState.INELIGIBLE)],
)
def test_research_eligibility_fails_closed(
    eligible: bool | None, expected: S5BaseAdmissionState
) -> None:
    result = build_s5_base_decision(
        as_of=AS_OF,
        sectors=(_observation("BANK"),),
        candidates=(_candidate("000001.SZ", research_eligible=eligible),),
    ).candidate_results[0]
    assert result.state is expected


def test_stock_building_is_ineligible() -> None:
    stock = _observation("000001.SZ", recovery_from_10d_low=0.01)
    result = build_s5_base_decision(
        as_of=AS_OF,
        sectors=(_observation("BANK"),),
        candidates=(_candidate("000001.SZ", observation=stock),),
    ).candidate_results[0]
    assert result.base_state is S5BaseState.BASE_BUILDING
    assert result.state is S5BaseAdmissionState.INELIGIBLE


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        (
            {"recent_return_10": None, "history_complete": False},
            S5BaseAdmissionState.UNKNOWN,
        ),
        ({"max_new_low_undercut_10": 0.10}, S5BaseAdmissionState.INELIGIBLE),
    ],
)
def test_stock_unknown_and_failed_are_not_selected(
    changes: dict[str, object], expected: S5BaseAdmissionState
) -> None:
    stock = _observation("000001.SZ", **changes)
    result = build_s5_base_decision(
        as_of=AS_OF,
        sectors=(_observation("BANK"),),
        candidates=(_candidate("000001.SZ", observation=stock),),
    ).candidate_results[0]
    assert result.state is expected
    assert result.selected is False


def test_confirmed_ranks_before_ready_then_features_and_id() -> None:
    ready_high = _candidate(
        "000002.SZ", observation=_observation("000002.SZ", recovery_from_10d_low=0.20)
    )
    confirmed = _candidate(
        "000003.SZ",
        observation=_observation(
            "000003.SZ", breakout_distance_20=0.01, volume_ratio_5_to_20=1.2
        ),
    )
    tied_a = _candidate("000001.SZ")
    decision = build_s5_base_decision(
        as_of=AS_OF,
        sectors=(_observation("BANK"),),
        candidates=(ready_high, confirmed, tied_a),
    )
    assert decision.selected_ids == ("000003.SZ", "000002.SZ", "000001.SZ")
    assert [r.rank for r in decision.candidate_results] == [3, 2, 1]


def test_input_order_invariance_and_max_names() -> None:
    candidates = tuple(_candidate(f"00000{i}.SZ") for i in range(1, 4))
    config = S5BaseDecisionConfig(max_names=2, weight_per_name=0.4)
    left = build_s5_base_decision(
        as_of=AS_OF,
        sectors=(_observation("BANK"),),
        candidates=candidates,
        config=config,
    )
    right = build_s5_base_decision(
        as_of=AS_OF,
        sectors=(_observation("BANK"),),
        candidates=tuple(reversed(candidates)),
        config=config,
    )
    assert left == right
    assert left.selected_ids == ("000001.SZ", "000002.SZ")
    assert left.target.cash_weight == pytest.approx(0.2)


def test_rejects_duplicate_and_date_or_identity_mismatch() -> None:
    candidate = _candidate("000001.SZ")
    with pytest.raises(ValueError, match="duplicate instrument_id"):
        build_s5_base_decision(
            as_of=AS_OF,
            sectors=(_observation("BANK"),),
            candidates=(candidate, candidate),
        )
    with pytest.raises(ValueError, match="duplicate sector_id"):
        build_s5_base_decision(
            as_of=AS_OF,
            sectors=(_observation("BANK"), _observation("BANK")),
            candidates=(),
        )
    with pytest.raises(ValueError, match="as_of"):
        build_s5_base_decision(
            as_of=AS_OF,
            sectors=(replace(_observation("BANK"), as_of=date(2026, 9, 13)),),
            candidates=(),
        )
    with pytest.raises(ValueError, match="entity_id"):
        replace(candidate, observation=_observation("OTHER"))
    with pytest.raises(ValueError, match="membership instrument_id"):
        replace(candidate, membership=_membership("000002.SZ"))


def test_config_rejects_overallocation() -> None:
    with pytest.raises(ValueError, match="cannot exceed"):
        S5BaseDecisionConfig(max_names=3, weight_per_name=0.34)
