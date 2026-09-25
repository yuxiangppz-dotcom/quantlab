"""Readiness, immutable recovery snapshots and explicit data-revision planning."""

import json
import shutil
from contextlib import ExitStack
from datetime import UTC, date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from quantlab.pipeline.config import load_project
from quantlab.pipeline.strategy import load_strategy
from quantlab.research.alpha158_store import exclusive_job
from quantlab.research.ml.artifacts import complete, verify_completed
from quantlab.research.ml.io import sha256, write_json


def health(project):
    from quantlab.pipeline.workflow import status

    state = status(project)
    reasons = [f"missing:{key}" for key in state["missing_evidence"]]
    reasons += [f"data:{r['session']}" for r in state["data"]["failed"]]
    if state["data"].get("missing_sessions"):
        reasons.append("incomplete requested data interval")
    account = state["stages"]["account"]
    if not isinstance(account, dict) or not account.get("forward_decision"):
        reasons.append("no qualified forward account decision")
    if isinstance(account, dict) and account.get("stale"):
        reasons.append("account is stale")
    last = state.get("latest_operation")
    if last and last["status"] == "blocked":
        reasons.append(f"last operation blocked:{last['action']}")
    return {
        "status": "blocked" if reasons else "healthy",
        "reasons": reasons,
        "reason": "; ".join(reasons) or None,
        "details": state,
        "execution_authority": False,
    }


def backup(project_path, destination):
    """Copy configured nonsecret data/artifacts, verify, then atomically publish.

    Restore is deliberately not in-place: verify the backup, restore to an isolated
    machine/path, preserve original paths or explicitly migrate absolute bindings.
    """
    project = load_project(project_path)
    strategy = load_strategy(project)
    roots = {
        key: project[key]
        for key in ("canonical", "raw", "receipts", "workspace", "account", "registry")
    }
    roots.update(
        {
            key: project[key]
            for key in ("ml_config", "availability", "execution_policy", "corporate_actions")
        }
    )
    roots["project.json"] = Path(project_path).resolve()
    if strategy:
        roots["strategy.json"] = project["strategy"]
        roots.update({key: strategy[key] for key in ("membership", "industries", "event_coverage")})
    else:
        roots["context"] = project["context"]
    destination = Path(destination).resolve()
    if any(
        destination == p or destination in p.parents or p in destination.parents
        for p in roots.values()
    ):
        raise ValueError("backup destination must be disjoint from every source")
    if destination.exists():
        raise FileExistsError("backup destination already exists")
    # Research-product backups must not force a paper account into existence:
    # an uninitialized account/registry is recorded instead of copied.
    optional = {"account", "registry"}
    not_initialized = sorted(key for key in optional if not roots[key].exists())
    for key in not_initialized:
        del roots[key]
    missing = [key for key, path in roots.items() if not path.exists()]
    if missing:
        raise ValueError(f"backup sources missing:{missing}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        for key in ("account", "registry", "receipts"):
            if key in roots:
                stack.enter_context(exclusive_job(roots[key]))
        temp = Path(stack.enter_context(TemporaryDirectory(dir=destination.parent)))
        stage = temp / "backup"
        stage.mkdir()
        sources = {}
        for name, root in roots.items():
            files = root.rglob("*") if root.is_dir() else [root]
            for source in files:
                if source.is_symlink():
                    raise ValueError("backup does not traverse symlinks")
                if not source.is_file() or source.name == "worker.lock":
                    continue
                relative = Path(name) / source.relative_to(root) if root.is_dir() else Path(name)
                before = sha256(source)
                target = stage / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                if sha256(target) != before or sha256(source) != before:
                    raise ValueError("backup source changed during copy")
                sources[str(source)] = {"file": str(relative), "sha256": before}
        if any(sha256(p) != r["sha256"] for p, r in sources.items()):
            raise ValueError("backup sources changed before publication")
        inventory = {
            str(p)
            for root in roots.values()
            for p in (root.rglob("*") if root.is_dir() else [root])
            if p.is_file() and p.name != "worker.lock"
        }
        if inventory != set(sources):
            raise ValueError("backup source inventory changed during copy")
        write_json(
            stage / "backup.json",
            {
                "schema": "quantlab_recovery_snapshot_v1",
                "created_at": datetime.now(UTC).isoformat(),
                "sources": sources,
                "not_initialized_roots": not_initialized,
                "restore_policy": "isolated_restore_then_verify_original_absolute_bindings",
            },
        )
        complete(stage)
        stage.rename(destination)
    return verify_backup(destination)


def verify_backup(path):
    path = Path(path)
    verify_completed(path)
    manifest = json.loads((path / "backup.json").read_text())
    for row in manifest["sources"].values():
        file = (path / row["file"]).resolve()
        if path.resolve() not in file.parents or sha256(file) != row["sha256"]:
            raise ValueError("backup file fingerprint/path mismatch")
    return {
        "status": "complete",
        "path": str(path),
        "files": len(manifest["sources"]),
        "not_initialized_roots": manifest.get("not_initialized_roots", []),
        "restore_drill_completed": False,
    }


def revision_plan(project, start, end):
    """Describe rebuild dependencies; never overwrite a sealed input or an account."""
    if start > end:
        raise ValueError("revision dates out of order")
    from quantlab.data.storage import ParquetStorage
    from quantlab.pipeline.ingestion import session_files

    storage = ParquetStorage(project["canonical"])
    changed = []
    for path in sorted((project["receipts"] / "sessions").glob("*.json")):
        day = date.fromisoformat(path.stem)
        if not start <= day <= end:
            continue
        receipt = json.loads(path.read_text())
        for key, file in session_files(storage, day).items():
            current = sha256(file) if file.exists() else None
            if current != receipt["files"][key]:
                changed.append(
                    {
                        "session": str(day),
                        "partition": key,
                        "sealed": receipt["files"][key],
                        "current": current,
                    }
                )
    return {
        "status": "planned",
        "start": str(start),
        "end": str(end),
        "changed_partitions": changed,
        "rebuild": [
            "new canonical/raw/receipts namespace",
            "historical CSI800 context",
            "feature windows touching revisions",
            "labels using revised endpoints",
            "affected folds, cost scenarios, reports and releases",
        ],
        "existing_accounts": "preserve; reconcile corrections before any migration",
        "writes_performed": False,
    }
