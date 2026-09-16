"""Registered cost/capital stress runs and evidence-bound paper-model admission."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from quantlab.pipeline.strategy import load_strategy
from quantlab.research.alpha158_store import exclusive_job
from quantlab.research.ml.artifacts import complete, verify_completed
from quantlab.research.ml.io import sha256, write_json


def stress(project, *, resume=False):
    from quantlab.pipeline.workflow import universe_inputs
    from quantlab.research.ml.cli import dispatch, parser

    strategy = load_strategy(project)
    if not strategy:
        raise ValueError("stress requires a predeclared v2 strategy")
    root = project["workspace"]
    verify_completed(root / "market")
    verify_completed(root / "training")
    origin = root / "market/market.jsonl"
    baseline_hash = sha256(origin)
    sessions = json.loads((root / "bundle/calendar.json").read_text())
    first = next(d for d in sessions if d >= project["test_start"])
    start = sessions[sessions.index(first) + 1]
    results = []
    for bps in strategy["slippage_bps"]:
        folder = root / "stress" / f"slippage-{bps}bp"
        inputs = folder / "inputs"
        expected = {
            "base_market_sha256": baseline_hash,
            "slippage_bps_each_side": bps,
            "strategy_sha256": strategy["strategy_sha256"],
        }
        with exclusive_job(folder):
            if inputs.exists():
                verify_completed(inputs)
                if json.loads((inputs / "intent.json").read_text()) != expected:
                    raise ValueError("stress specification changed; select a new workspace")
            else:
                with TemporaryDirectory(dir=folder) as temp:
                    stage = Path(temp) / "inputs"
                    stage.mkdir()
                    with origin.open() as source, (stage / "market.jsonl").open("x") as dest:
                        for line in source:
                            payload = json.loads(line)
                            for context in payload["contexts"]:
                                if context["fees"] is None:
                                    raise ValueError("stress cannot fabricate missing fee evidence")
                                context["fees"]["adverse_slippage_rate"] = str(Decimal(bps) / 10000)
                                context["fees"]["scenario_id"] += f":stress-{bps}bp"
                            dest.write(json.dumps(payload) + "\n")
                    if sha256(origin) != baseline_hash:
                        raise ValueError("baseline market changed during stress preparation")
                    write_json(stage / "intent.json", expected)
                    complete(stage)
                    stage.rename(inputs)
        argv = [
            "replay",
            "--bundle",
            str(root / "bundle"),
            "--config",
            str(project["ml_config"]),
            "--start",
            start,
            "--end",
            project["test_end"],
            "--require-lineage",
            "--run",
            str(root / "training"),
            "--market-days",
            str(inputs / "market.jsonl"),
            "--initial-marks",
            str(root / "market/initial_marks.json"),
            "--corporate-actions",
            str(root / "market/corporate_actions.json"),
            "--capital-cny",
            *map(str, strategy["capital_scenarios_cny"]),
            "--output",
            str(folder / "replay"),
        ]
        if resume:
            argv += ["--resume"]
        dispatch(parser().parse_args(argv))
        report_path = folder / "report"
        if report_path.exists():
            verify_completed(report_path)
        else:
            dispatch(
                parser().parse_args(
                    [
                        "report",
                        "--replay",
                        str(folder / "replay"),
                        "--benchmark",
                        str(root / "market/benchmark.parquet"),
                        "--training",
                        str(root / "training"),
                        "--exposures",
                        str(universe_inputs(project) / "exposures.parquet"),
                        "--output",
                        str(report_path),
                    ]
                )
            )
        results.append(
            {
                "bps": bps,
                "report": str(report_path),
                "sha256": sha256(report_path / "completed.json"),
            }
        )
    return {"status": "complete", "scenarios": results, "execution_authority": False}


def assess_reports(reports, kind, strategy, expected_start, expected_end):
    """Policy thresholds are declared research choices, not universal IC pass marks."""
    failures = []
    criteria = strategy["release"]
    for name, report in reports:
        statuses = [r for r in report["scenario_statuses"] if r["model"] == kind]
        if {r["capital_fen"] for r in statuses} != {
            n * 100 for n in strategy["capital_scenarios_cny"]
        }:
            failures.append(f"{name}:missing capital scenarios")
        for row in statuses:
            if row["stop_reason"] or row["valid_through"] != expected_end:
                failures.append(f"{name}:incomplete replay")
        selected = [r for r in report["scenarios"] if r["scenario"].startswith(f"{kind}-")]
        if len(selected) != len(strategy["capital_scenarios_cny"]):
            failures.append(f"{name}:missing report scenarios")
        for row in selected:
            if (
                row["first_session"] != expected_start
                or row["last_session"] != expected_end
                or row["excluded_sessions"]
                or row["sessions"] < criteria["min_sessions"]
            ):
                failures.append(f"{name}:insufficient/partial evaluation")
            if (
                row["max_drawdown"] < -criteria["max_drawdown"]
                or row["relative_wealth_return"] < criteria["min_relative_return"]
            ):
                failures.append(f"{name}:economic thresholds")
            if not row.get("mean_gross_exposure") or row["risk_breach_days"]:
                failures.append(f"{name}:no invested evidence or risk breaches")
        interval = report.get("signal_uncertainty", {}).get(kind, {})
        if interval.get("status") != "computed" or interval.get("mean", 0) <= 0:
            failures.append(f"{name}:insufficient/negative signal evidence")
        if not report.get("exposures_sha256"):
            failures.append(f"{name}:style coverage report absent")
        if any(r.get("style_unknown_days", 1) for r in selected):
            failures.append(f"{name}:unknown style exposure")
    return sorted(set(failures))


def release_model(project, identifier):
    from quantlab.research.ml.serving import _model_folder

    strategy = load_strategy(project)
    if not strategy:
        raise ValueError("release requires v2 strategy")
    root = project["workspace"]
    registered = _model_folder(project["registry"], identifier)
    model = json.loads((registered / "registration.json").read_text())
    verify_completed(root / "training")
    training_hash = sha256(root / "training/completed.json")
    if model["source_run_sha256"] != training_hash:
        raise ValueError("candidate is not from this evaluated training run")
    trials = list((root / "study/trials").glob("*.json"))
    if not trials:
        raise ValueError("registered research protocol/trial required")
    study_binding = json.loads((root / "study/strategy.json").read_text())
    if study_binding["sha256"] != strategy["strategy_sha256"]:
        raise ValueError("release strategy differs from predeclared study")
    intent = json.loads((root / "training/intent.json").read_text())
    matching = []
    for path in trials:
        trial = json.loads(path.read_text())
        spec = trial["specification"]
        if (
            spec["config"] == intent["config"]
            and spec["manifest"] == intent["inputs"]
            and spec["code"] == intent["code"]
            and spec["start"] == intent["start"]
            and spec["end"] == intent["end"]
        ):
            matching.append(trial)
    if not matching:
        raise ValueError("no registered trial matches the evaluated training run")
    reports, bindings = [], {str(root / "training/completed.json"): training_hash}
    for folder in [
        root / "report",
        *[root / "stress" / f"slippage-{n}bp/report" for n in strategy["slippage_bps"]],
    ]:
        verify_completed(folder)
        report = json.loads((folder / "report.json").read_text())
        if report["training_completion_sha256"] != training_hash:
            raise ValueError("release report references a different training run")
        reports.append((str(folder), report))
        bindings[str(folder / "completed.json")] = sha256(folder / "completed.json")
    sessions = json.loads((root / "bundle/calendar.json").read_text())
    first = next(d for d in sessions if d >= project["test_start"])
    failures = assess_reports(
        reports, model["kind"], strategy, sessions[sessions.index(first) + 1], project["test_end"]
    )
    if failures:
        return {"status": "blocked", "reason": "; ".join(failures), "model_id": identifier}
    payload = {
        "schema": "quantlab_paper_release_v1",
        "model_id": identifier,
        "registration_sha256": sha256(registered / "completed.json"),
        "strategy_sha256": strategy["strategy_sha256"],
        "evidence": bindings,
        "created_at": datetime.now(UTC).isoformat(),
        "execution_authority": False,
        "approval_scope": "paper_observation_only",
        "performance_certified": False,
        "research_protocol": json.loads((root / "study/study.json").read_text()),
        "matched_trials": matching,
    }
    destination = project["registry"] / "releases" / identifier
    with exclusive_job(project["registry"]):
        if destination.exists():
            return verify_release(project["registry"], identifier, strategy["strategy_sha256"])
        destination.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=destination.parent) as temp:
            stage = Path(temp) / "release"
            stage.mkdir()
            write_json(stage / "release.json", payload)
            complete(stage)
            stage.rename(destination)
    return payload


def verify_release(registry, identifier, strategy_hash, *, cutoff=None):
    from quantlab.research.ml.serving import _model_folder

    model = _model_folder(registry, identifier)
    folder = registry / "releases" / identifier
    verify_completed(folder)
    release = json.loads((folder / "release.json").read_text())
    if (
        release["model_id"] != identifier
        or release["strategy_sha256"] != strategy_hash
        or release["registration_sha256"] != sha256(model / "completed.json")
    ):
        raise ValueError("release does not bind this model/strategy")
    if cutoff is not None:
        import pandas as pd

        if pd.Timestamp(release["created_at"]) > cutoff:
            raise ValueError("release was unavailable at decision cutoff")
    for path, digest in release["evidence"].items():
        verify_completed(Path(path).parent)
        if sha256(path) != digest:
            raise ValueError("release evidence changed")
    return release
