"""Study-wide attempt accounting and provisional, assumption-labelled DSR reports.

An invocation/retry is not another independent experiment. Distinct registered
specifications and each model within them count separately. Failed/unreported
candidates remain in the denominator; incomplete comparisons never produce DSR.
"""

import json
from statistics import stdev

import pandas as pd

from quantlab.research.alpha158_store import exclusive_job
from quantlab.research.ml.artifacts import fingerprint, verify_completed
from quantlab.research.ml.io import sha256, write_json
from quantlab.research.ml.statistics import deflated_sharpe, probabilistic_sharpe, return_moments


def candidate_key(specification, model):
    # Moving the identical run to another output directory is not another search.
    spec = {k: v for k, v in specification.items() if k != "output"}
    return fingerprint({"specification": spec, "model": model})


def trial_inventory(study):
    paths = sorted((study / "trials").glob("*.json"))
    candidates = {}
    for path in paths:
        row = json.loads(path.read_text())
        if fingerprint(row["specification"]) != row["specification_sha256"]:
            # Pre-normalization v1 reserved MLConfig.models as a tuple before JSON save.
            legacy = json.loads(json.dumps(row["specification"]))
            legacy["config"]["models"] = tuple(legacy["config"]["models"])
            if fingerprint(legacy) != row["specification_sha256"]:
                raise ValueError("trial specification hash mismatch")
        for model in row["specification"]["config"]["models"]:
            key = candidate_key(row["specification"], model)
            candidates[key] = {"model": model, "specification": row["specification"]}
    return {
        "registered_invocations": len(paths),
        "registered_candidate_count": len(candidates),
        "candidate_ids": sorted(candidates),
        "trial_files": {p.name: sha256(p) for p in paths},
        "count_scope": "this_study_only_including_failed_and_unreported_candidates",
        "unregistered_and_prior_trials_known": False,
        "independent_trial_count_known": False,
    }, candidates


def selection_diagnostics(study, training, replay):
    verify_completed(training)
    verify_completed(replay)
    intent = json.loads((training / "intent.json").read_text())
    replay_intent = json.loads((replay / "intent.json").read_text())["inputs"]
    if replay_intent["extra"]["training_completion_sha256"] != sha256(training / "completed.json"):
        raise ValueError("selection replay does not bind evaluated training")
    if replay_intent["strategy_mode"] != "backtest":
        raise ValueError("selection needs model replay, not a baseline")
    statuses = json.loads((replay / "summary.json").read_text())
    with exclusive_job(study):
        inventory, candidates = trial_inventory(study)
        matching = {}
        for key, candidate in candidates.items():
            spec = candidate["specification"]
            expected = {
                "config": intent["config"],
                "manifest": intent["inputs"],
                "code": intent["code"],
                "start": intent["start"],
                "end": intent["end"],
            }
            if {k: v for k, v in spec.items() if k != "output"} == expected:
                matching[candidate["model"]] = key
        if set(intent["config"]["models"]) != set(matching):
            raise ValueError("all evaluated models must match registered study trials")
        for status in statuses:
            model, capital = status["model"], status["capital_fen"]
            if model not in matching:
                raise ValueError("replay model was not preregistered")
            name = f"{model}-{capital}fen"
            ledger = pd.read_parquet(replay / name / "ledger.parquet")
            payload = {
                "candidate_id": matching[model],
                "capital_fen": capital,
                "replay_completion_sha256": sha256(replay / "completed.json"),
                "status": status,
                "sessions": ledger.session.tolist() if len(ledger) else [],
                "returns": ledger.daily_return.tolist() if len(ledger) else [],
            }
            # Content address preserves repeats/stresses/code identities instead of overwriting.
            path = study / "evaluations" / f"{fingerprint(payload)}.json"
            if not path.exists():
                write_json(path, payload)
        stored = []
        evidence = {}
        for path in sorted((study / "evaluations").glob("*.json")):
            payload = json.loads(path.read_text())
            if fingerprint(payload) != path.stem:
                raise ValueError("study evaluation content hash mismatch")
            stored.append(payload)
            evidence[path.name] = sha256(path)
        output = {}
        for status in statuses:
            model, capital = status["model"], status["capital_fen"]
            current = next(
                r
                for r in stored
                if r["candidate_id"] == matching[model]
                and r["capital_fen"] == capital
                and r["replay_completion_sha256"] == sha256(replay / "completed.json")
            )
            row = {
                "status": "insufficient_comparable_trials",
                "psr_iid_zero_reference": None,
                "dsr_iid_raw_trial_proxy": None,
                "missing_or_ambiguous_candidates": [],
                "independence_assumed_not_verified": True,
                "serial_dependence_corrected": False,
                "multiple_testing_adjusted": False,
                "return_basis": "net_daily_returns_zero_risk_free_rate",
                "replay_complete": not bool(status["stop_reason"])
                and status["valid_through"] == replay_intent["end"],
            }
            comparable = []
            for key in candidates:
                options = [
                    r
                    for r in stored
                    if r["candidate_id"] == key
                    and r["capital_fen"] == capital
                    and r["sessions"] == current["sessions"]
                    and not r["status"]["stop_reason"]
                    and r["status"]["valid_through"] == replay_intent["end"]
                ]
                # Multiple different return paths for one specification are not cherry-picked.
                paths = {fingerprint(r["returns"]): r for r in options}
                if len(paths) != 1:
                    row["missing_or_ambiguous_candidates"].append(key)
                    continue
                try:
                    comparable.append(return_moments(next(iter(paths.values()))["returns"]))
                except ValueError:
                    row["missing_or_ambiguous_candidates"].append(key)
            try:
                moments = return_moments(current["returns"])
                row["moments"] = moments
                args = (
                    moments["sharpe_per_observation"],
                    moments["observations"],
                    moments["skewness"],
                    moments["pearson_kurtosis"],
                )
                row["psr_iid_zero_reference"] = probabilistic_sharpe(*args)
                if not row["missing_or_ambiguous_candidates"] and len(comparable) >= 2:
                    spread = stdev(r["sharpe_per_observation"] for r in comparable)
                    row["dsr_iid_raw_trial_proxy"] = deflated_sharpe(
                        *args, trials=len(candidates), trial_sharpe_std=spread
                    )
                    row["trial_sharpe_std_per_observation"] = spread
                    row["status"] = "computed_under_unverified_iid_raw_count_assumptions"
            except ValueError as exc:
                row["status"] = "undefined_sharpe"
                row["reason"] = str(exc)
            output[f"{model}-{capital}fen"] = row
        return {
            "inventory": inventory,
            "scenarios": output,
            "evaluations": evidence,
            "performance_certified": False,
            "scope_warning": "other studies/manual searches unknown; retry is not independence",
        }
