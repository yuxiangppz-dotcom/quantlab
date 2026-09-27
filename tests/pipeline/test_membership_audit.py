"""Behavioral regressions for the offline membership diagnostic, not PIT admission."""

import importlib.util
import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/audit_csi800_membership.py"
spec = importlib.util.spec_from_file_location("membership_audit", SCRIPT)
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)
LOWER, DAY, UPPER = date(2024, 6, 1), date(2024, 6, 15), date(2024, 6, 30)


def event(action="add", src="one", day=DAY, code="A", **extra):
    return {"date": day, "code": code, "action": action, "src": src, **extra}


@pytest.mark.parametrize("sources", [("one", "two"), ("one", "one")])
def test_duplicate_provenance_never_reexecutes(sources):
    result = audit.decompose_transition(set(), {"A"}, [event(src=s) for s in sources], LOWER, UPPER)
    assert result["classification"] == "endpoint_match_clean"
    assert result["derived_endpoint"] == ["A"]
    assert result["precondition_failures"] == []
    assert len(result["results"]) == 1
    assert result["results"][0]["sources"] == sorted(set(sources))


@pytest.mark.parametrize("sources", [("a", "z"), ("z", "a")])
@pytest.mark.parametrize("reverse", [False, True])
def test_same_day_opposites_are_unknown_regardless_of_source_order(sources, reverse):
    events = [event("add", sources[0]), event("remove", sources[1])]
    if reverse:
        events.reverse()
    result = audit.decompose_transition(set(), set(), events, LOWER, UPPER)
    assert result["classification"] == "event_conflict"
    assert result["derived_endpoint"] is None
    assert result["results"] == []
    assert {c["action"] for c in result["event_conflicts"][0]["candidates"]} == {"add", "remove"}


def test_distinct_date_round_trip_is_chronological():
    events = [event("remove", day=date(2024, 6, 20)), event("add")]
    result = audit.decompose_transition(set(), set(), events, LOWER, UPPER)
    assert result["classification"] == "valid_intra_month_round_trip"
    assert result["derived_endpoint"] == []
    assert result["precondition_failures"] == []
    assert [(r["action"], r["outcome"]) for r in result["results"]] == [
        ("add", "applied"),
        ("remove", "applied"),
    ]


def test_same_endpoint_does_not_hide_invalid_event():
    result = audit.decompose_transition(set(), set(), [event("remove")], LOWER, UPPER)
    assert result["classification"] == "invalid_events"
    assert len(result["precondition_failures"]) == 1


def test_conflicting_terms_discard_all_candidates():
    result = audit.decompose_transition(
        set(), {"A"}, [event(version="one"), event(src="two", version="two")], LOWER, UPPER
    )
    assert result["classification"] == "event_conflict"
    assert result["results"] == []
    assert len(result["event_conflicts"][0]["candidates"]) == 2


def test_distinct_codes_same_date_commute():
    events = [event("remove", "z", code="A"), event("add", "a", code="B")]
    a = audit.decompose_transition({"A"}, {"B"}, events, LOWER, UPPER)
    b = audit.decompose_transition({"A"}, {"B"}, events[::-1], LOWER, UPPER)
    assert a == b
    assert a["classification"] == "endpoint_match_clean"


def test_large_residual_sets_are_complete():
    start = {f"A{i}" for i in range(35)}
    end = {f"B{i}" for i in range(40)}
    result = audit.decompose_transition(start, end, [], LOWER, UPPER)
    assert result["classification"] == "missing_events"
    assert set(result["unexplained_in"]) == end
    assert set(result["unexplained_out"]) == start


def write_observation(root, name, codes, day="20240615"):
    path = root / name / "weights.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"trade_date": [day] * len(codes), "con_code": codes}).to_parquet(path)
    return path


def test_conflicting_observations_block_cli_and_preserve_both_sources(tmp_path):
    obs = tmp_path / "observations"
    a = write_observation(obs, "one", ["000001.SZ"])
    b = write_observation(obs, "two", ["000002.SZ"])
    out = tmp_path / "output"
    argv = [
        "--observations",
        str(obs),
        "--facts-root",
        str(tmp_path / "absent"),
        "--intake-root",
        str(tmp_path / "also-absent"),
        "--output",
        str(out),
    ]
    assert audit.main(argv) == 2
    result = json.loads((out / "audit.json").read_text())
    assert result["status"] == "blocked_observation_conflict"
    assert result["rows"] == []
    issue = result["same_day_observation_conflicts"][0]
    assert {v["path"] for v in issue["observations"]} == {str(a), str(b)}
    assert issue["diff_codes"] == ["000001.SZ", "000002.SZ"]
    assert all(
        v["sha256"] == audit.digest(Path(v["path"]).read_bytes()) for v in issue["observations"]
    )
    original = (out / "audit.json").read_bytes()
    with pytest.raises(FileExistsError):
        audit.main(argv)
    assert (out / "audit.json").read_bytes() == original


def test_actual_observation_content_changes_bound_hash(tmp_path):
    path = write_observation(tmp_path, "one", ["000001.SZ"])
    before, hashes_before, _ = audit.load_observations(tmp_path)
    write_observation(tmp_path, "one", ["000002.SZ"])
    after, hashes_after, _ = audit.load_observations(tmp_path)
    assert before != after
    assert hashes_before[str(path)] != hashes_after[str(path)]
    assert hashes_after[str(path)] == audit.digest(path.read_bytes())


def test_identical_same_day_observations_preserve_every_input_hash(tmp_path):
    a = write_observation(tmp_path, "a", ["000001.SZ"])
    b = write_observation(tmp_path, "b", ["000001.SZ"])
    obs, hashes, conflicts = audit.load_observations(tmp_path)
    assert len(obs) == 1
    assert conflicts == []
    assert set(hashes) == {str(a), str(b)}


def test_fact_binding_uses_exact_read_bytes(tmp_path):
    path = tmp_path / "facts.json"
    path.write_text('{"effective_date": "2024-06-15"}')
    bindings = {}
    assert audit.read_fact(path, bindings) == {"effective_date": "2024-06-15"}
    assert bindings == {str(path): audit.digest(path.read_bytes())}


def test_report_lists_all_problem_windows_from_rows():
    rows = [
        {
            "to": "2024-01-31",
            "classification": "missing_events",
            "unexplained_in": ["A"],
            "unexplained_out": ["B"],
        }
    ]
    report = audit.render_report(
        {"status": "diagnostic_only", "rows": rows, "classification_counts": {"missing_events": 1}}
    )
    assert "| 2024-01-31 | missing_events | 1 | 1 |" in report
