"""Candidate refits under a previously admitted, frozen paper research method.

This command never activates a candidate or claims a fresh one-day backtest proves
profitability. Strategy changes require a new study and full stress/release path.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd

from quantlab.data.storage import ParquetStorage
from quantlab.pipeline.research import verify_release
from quantlab.pipeline.strategy import load_strategy
from quantlab.research.alpha158_store import exclusive_job
from quantlab.research.ml.artifacts import complete, verify_completed
from quantlab.research.ml.config import load_config
from quantlab.research.ml.io import sha256, verify_bundle, write_json
from quantlab.research.ml.serving import _model_folder, register_model


def refresh(project, asof, parent_id, *, resume=False):
    from quantlab.pipeline.workflow import ROOT, build
    from quantlab.research.ml.runner import code_identity, run_training

    if asof > pd.Timestamp.now(tz="Asia/Shanghai").date():
        raise ValueError("refit cannot use a future session")
    strategy = load_strategy(project)
    if not strategy:
        raise ValueError("candidate refresh requires a frozen v2 strategy")
    admitted = verify_release(project["registry"], parent_id, strategy["strategy_sha256"])
    parent = _model_folder(project["registry"], parent_id)
    previous = json.loads((parent / "registration.json").read_text())
    original = [
        Path(p).parent
        for p, digest in admitted["evidence"].items()
        if digest == previous["source_run_sha256"]
    ]
    if len(original) != 1 or json.loads((original[0] / "intent.json").read_text())[
        "code"
    ] != code_identity(ROOT):
        raise ValueError("refit code/runtime changed; requalify the method with a full study")
    config = load_config(project["ml_config"])
    if json.loads(json.dumps(config.payload())) != previous["config"]:
        raise ValueError("refit cannot change frozen model/portfolio parameters")
    if asof <= pd.Timestamp(previous["fit"]["test_start"]).date():
        raise ValueError("candidate refit must advance the observation date")
    calendar = ParquetStorage(project["canonical"]).load_trading_calendar()
    sessions = sorted({c.trade_date for c in calendar if c.is_open})
    if asof not in sessions:
        raise ValueError("refit date is not a verified trading session")
    left = sessions.index(asof) - config.train_sessions - config.validation_sessions
    if left < 0:
        raise ValueError("insufficient refit training/validation history")
    folder = project["workspace"] / "updates" / str(asof)
    # The feature builder locks its output parent; use a distinct orchestration
    # lock to avoid competing with our own nested stage in the same process.
    with exclusive_job(folder / ".orchestration"):
        intent = {
            "parent_model_id": parent_id,
            "strategy_sha256": strategy["strategy_sha256"],
            "asof": str(asof),
            "selection": "same admitted model kind; no parameter search",
        }
        if (folder / "intent.json").exists():
            if json.loads((folder / "intent.json").read_text()) != intent:
                raise ValueError("refit request changed")
        else:
            write_json(folder / "intent.json", intent)
        bundle = folder / "bundle"
        if bundle.exists():
            verify_bundle(bundle)
        else:
            build(project, start=sessions[left], end=asof, output=bundle)
        if sha256(bundle / "feature_contract.json") != previous["feature_contract_sha256"]:
            raise ValueError("refit feature/universe contract differs from admitted method")
        run_training(
            bundle, project["ml_config"], folder / "training", asof, asof, root=ROOT, resume=resume
        )
        model = register_model(
            folder / "training", asof.strftime("%Y-%m"), previous["kind"], project["registry"]
        )
        fit = model["fit"]
        # Diagnostics use mature validation labels only. Today's prediction labels are unknown.
        if (
            fit.get("validation_rank_ic") is None
            or fit["validation_rank_ic"] <= 0
            or fit.get("validation_ic_days", 0) < min(60, config.validation_sessions // 2)
        ):
            return {
                "status": "blocked",
                "reason": "candidate validation signal insufficient/nonpositive",
                "model_id": model["model_id"],
                "active_model_changed": False,
            }
        target = project["registry"] / "releases" / model["model_id"]
        payload = {
            "schema": "quantlab_paper_release_v1",
            "model_id": model["model_id"],
            "registration_sha256": sha256(
                project["registry"] / "models" / model["model_id"] / "completed.json"
            ),
            "strategy_sha256": strategy["strategy_sha256"],
            "created_at": datetime.now(UTC).isoformat(),
            "parent_model_id": parent_id,
            "approval_scope": "same_method_refit_paper_observation_only",
            "performance_certified": False,
            "execution_authority": False,
            "evidence": {
                **admitted["evidence"],
                str(project["registry"] / "releases" / parent_id / "completed.json"): sha256(
                    project["registry"] / "releases" / parent_id / "completed.json"
                ),
                str(folder / "training/completed.json"): sha256(folder / "training/completed.json"),
            },
        }
        with exclusive_job(project["registry"]):
            if target.exists():
                verify_release(project["registry"], model["model_id"], strategy["strategy_sha256"])
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with TemporaryDirectory(dir=target.parent) as temp:
                    stage = Path(temp) / "release"
                    stage.mkdir()
                    write_json(stage / "release.json", payload)
                    complete(stage)
                    stage.rename(target)
        verify_completed(folder / "training")
        return {
            "status": "candidate_ready",
            "model_id": model["model_id"],
            "active_model_changed": False,
            "validation_rank_ic": fit["validation_rank_ic"],
        }
