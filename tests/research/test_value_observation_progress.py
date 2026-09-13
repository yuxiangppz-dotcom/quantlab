import json

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from quantlab.data.models import DataValidationError, canonical_payload_fingerprint
from quantlab.research import value_observation_progress as m
from quantlab.research.input_audit import _sha


@pytest.fixture
def saved(tmp_path, monkeypatch):
    bundles = {key: (key, {}) for key in ("value", "calendar", "closing")}
    monkeypatch.setattr(m, "BUNDLES", bundles)
    bodies = {}

    def write(key, name, body):
        bodies[key, name] = body
        p = tmp_path / key / name
        p.parent.mkdir(exist_ok=True)
        fp = canonical_payload_fingerprint(body)
        p.write_text(json.dumps({**body, "fingerprint": fp}), encoding="utf-8")
        bundles[key][1][name] = fp
        return fp

    reasons = {
        "observed_value_score": 142,
        "valuation_unknown_or_nonpositive": 84,
        "fewer_than20_valid_values_in_size_group": 19,
        "size_unknown_or_nonpositive": 11,
    }
    observation = {
        "identity": "S2-A:prospective-value-20260913-v1",
        "cohort_size": 256,
        "scores_known": 142,
        "records": [{"score_known": i < 142} for i in range(256)],
        "reasons": reasons,
        "created_at": "2026-09-13T03:22:53+00:00",
        "source_received_at": "2026-09-12T17:36:08+00:00",
        "future_labels_evaluated": False,
        "historical_pit_certified": False,
        "performance_evidence": False,
        "historical_performance_eligible": False,
        "execution_authority": False,
        "future_calendar_complete": False,
        "correlation": None,
        "future_exit_date": None,
        "label_status": "pending_future_calendar_and21_session_prices",
        "price_as_of": "2026-09-11",
        "earliest_entry_session": "2026-09-14",
        "rule_candidates_cumulative": 4,
        "economic_paths": 0,
        "model_fits": 0,
    }
    fp = write("value", "observation.json", observation)
    write(
        "value",
        "proof.json",
        {
            "observation_fingerprint": fp,
            "all_checks_passed": True,
            "checked_codes": 256,
            "scores_known": 142,
            "future_labels_evaluated": False,
            "execution_authority": False,
        },
    )
    return tmp_path, bodies, write


def add_calendar(root, write):
    dates = pd.bdate_range("2026-09-14", periods=21).strftime("%Y-%m-%d").tolist()
    # Synthetic weekday calendar only, not China's real holiday plan.
    files = {}
    for name in ("sse.raw", "szse.raw"):
        p = root / "calendar" / name
        p.parent.mkdir(exist_ok=True)
        p.write_bytes(b"synthetic")
        files[name] = {"sha256": _sha(p), "bytes": p.stat().st_size}
    fp = write(
        "calendar",
        "calendar_plan.json",
        {
            "observation_fingerprint": m.BUNDLES["value"][1]["observation.json"],
            "planned_label_dates": dates,
            "planned_entry": dates[0],
            "planned_end": dates[-1],
            "subsequent_sessions": 20,
            "requires_calendar_revalidation_at_evaluation": True,
            "future_actual_openings_certified": False,
            "future_labels_evaluated": False,
            "execution_authority": False,
            "performance_evidence": False,
            "raw_files": files,
        },
    )
    write(
        "calendar",
        "proof.json",
        {
            "report_fingerprint": fp,
            "all_checks_passed": True,
            "source_rows_checked": 102,
            "planned_end": dates[-1],
            "future_labels_evaluated": False,
            "execution_authority": False,
        },
    )


def test_missing_bundle_is_unknown(tmp_path):
    assert m.read_value_observation(tmp_path) is None
    assert m.read_closing_progress(tmp_path) is None


def test_bound_observation_retains_time_counts_and_pending_end(saved):
    root, _, _ = saved
    result = m.read_value_observation(root)
    assert result["known"] == 142 and result["cohort"] == 256
    assert result["planned_end"] is None
    assert result["created_at_china"] == "2026-09-13 11:22:53"


@pytest.mark.parametrize(
    "bad",
    [
        "partial",
        "corrupt",
        "authority",
        "profit",
        "evaluated",
        "cohort",
        "reasons",
        "missing_reason",
        "count",
        "late",
        "naive",
    ],
)
def test_unverified_semantics_never_display_as_success(saved, bad):
    root, bodies, write = saved
    report = bodies["value", "observation.json"]
    proof = bodies["value", "proof.json"]
    if bad == "partial":
        (root / "value/proof.json").unlink()
    elif bad == "corrupt":
        (root / "value/observation.json").write_text("{}")
    else:
        if bad in ("authority", "profit", "evaluated"):
            report[
                {
                    "authority": "execution_authority",
                    "profit": "performance_evidence",
                    "evaluated": "future_labels_evaluated",
                }[bad]
            ] = True
        elif bad == "cohort":
            report["records"].pop()
        elif bad == "reasons":
            report["reasons"]["observed_value_score"] = 143
        elif bad == "missing_reason":
            report["reasons"]["other"] = report["reasons"].pop("size_unknown_or_nonpositive")
        elif bad == "count":
            report["records"][0]["score_known"] = 1
        elif bad == "late":
            report["created_at"] = "2026-09-14T03:00:00+00:00"
        else:
            report["created_at"] = "2026-09-13T03:00:00"
        proof["observation_fingerprint"] = write("value", "observation.json", report)
        write("value", "proof.json", proof)
    with pytest.raises(DataValidationError):
        m.read_value_observation(root)


@pytest.mark.parametrize("bad", [None, "link", "future_fact", "short", "raw_corrupt"])
def test_planned_calendar_is_separate_and_still_pending(saved, bad):
    root, bodies, write = saved
    add_calendar(root, write)
    report, proof = bodies["calendar", "calendar_plan.json"], bodies["calendar", "proof.json"]
    if bad is None:
        result = m.read_value_observation(root)
        assert result["planned_end"] == "2026-10-12"  # synthetic calendar
        assert result["state"] == "已登记，未来结果尚未评价"
        return
    if bad == "raw_corrupt":
        (root / "calendar/sse.raw").write_bytes(b"changed")
    else:
        if bad == "link":
            report["observation_fingerprint"] = "wrong"
        elif bad == "future_fact":
            report["future_actual_openings_certified"] = True
        else:
            report["planned_label_dates"].pop()
        proof["report_fingerprint"] = write("calendar", "calendar_plan.json", report)
        write("calendar", "proof.json", proof)
    with pytest.raises(DataValidationError):
        m.read_value_observation(root)


@pytest.mark.parametrize("bad", [False, True])
def test_closing_quantity_coverage_never_grants_fills(saved, bad):
    root, _, write = saved
    fp = write(
        "closing",
        "report.json",
        {
            "covered": 2904,
            "rows": [None] * 2904,
            "sessions": 726,
            "unchanged_continuous_payloads": 2904,
            "execution_authority": bad,
            "performance_evidence": False,
        },
    )
    write(
        "closing",
        "proof.json",
        {
            "report_fingerprint": fp,
            "all_checks_passed": True,
            "checked_rows": 2904,
            "execution_authority": False,
            "performance_evidence": False,
        },
    )
    if bad:
        with pytest.raises(DataValidationError):
            m.read_closing_progress(root)
    else:
        assert m.read_closing_progress(root)["board_dates"] == 2904


def test_visible_counts_are_observations_not_recommendations(saved, monkeypatch):
    from quantlab.ui import research_program

    root, _, write = saved
    add_calendar(root, write)
    monkeypatch.setattr(research_program, "PROJECT_ROOT", root)
    app = AppTest.from_string(
        "from quantlab.ui.research_program import render_value_observation\n"
        "render_value_observation()"
    ).run(timeout=20)
    assert not app.exception and not app.error
    assert app.dataframe[0].value["数量"].tolist() == [256, 142, 84, 19, 11]
    text = " ".join(x.value for x in [*app.markdown, *app.caption])
    assert "未来结果尚未评价" in text and "不是推荐持仓" in text
    assert "20个共同交易日" in text and "累计4/15" in text
