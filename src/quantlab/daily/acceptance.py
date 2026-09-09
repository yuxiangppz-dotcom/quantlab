"""Compact end-to-end acceptance artifact for the local Daily v1 product."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pandas as pd

from quantlab.daily.experiments import load_baseline_view
from quantlab.daily.service import (
    PROJECT_ROOT,
    _atomic_write_text,
    inspect_data_status,
    load_latest_snapshot,
)
from quantlab.personal import list_accounts, load_latest_plan

DEFAULT_ACCEPTANCE_ROOT = PROJECT_ROOT / "data" / "products" / "acceptance"


def _git(command: str) -> str:
    return subprocess.check_output(["git", *command.split()], cwd=PROJECT_ROOT, text=True).strip()


def run_v1_acceptance(
    account_id: str = "demo_200k", *, output_root: Path = DEFAULT_ACCEPTANCE_ROOT
) -> tuple[Path, Path, dict]:
    status = inspect_data_status()
    snapshot = load_latest_snapshot()
    baseline = load_baseline_view()
    accounts = list_accounts()
    plan_item = load_latest_plan(account_id) if account_id in accounts else None
    checks = {
        "common_complete_data_date_available": status["effective_as_of"] is not None,
        "daily_snapshot_available": snapshot is not None,
        "daily_exports_available": bool(
            snapshot
            and all(
                path.exists()
                for path in (
                    snapshot.ranking_path,
                    snapshot.target_path,
                    snapshot.html_path,
                    snapshot.report_path,
                )
            )
        ),
        "formal_baseline_available": baseline is not None,
        "account_available": account_id in accounts,
        "reference_plan_available": plan_item is not None,
        "worktree_clean": not _git("status --porcelain"),
    }
    ranking_columns = []
    if snapshot is not None and snapshot.ranking_path.exists():
        ranking_columns = list(pd.read_csv(snapshot.ranking_path, nrows=1).columns)
    required_columns = {
        "instrument_id",
        "alpha_score",
        "selected",
        "target_weight",
        "risk_context",
    }
    checks["ranking_has_user_columns"] = required_columns.issubset(ranking_columns)
    core = {
        "schema": "quantlab_daily_v1_acceptance",
        "version": "1.0.0",
        "git_head": _git("rev-parse HEAD"),
        "checks": checks,
        "product_ready": all(checks.values()),
        "investment_strategy_promoted": False,
        "external_order_submission": False,
        "daily": (
            {
                "effective_as_of": snapshot.report["effective_as_of"],
                "config_id": snapshot.report["model"]["config_id"],
                "model_status": snapshot.report["model"]["model_status"],
                "content_fingerprint": snapshot.report["content_fingerprint"],
            }
            if snapshot
            else None
        ),
        "baseline": (
            {"schema": baseline["schema"], "run_id": baseline["run_id"]} if baseline else None
        ),
        "account_id": account_id,
        "plan_id": plan_item[1]["plan_id"] if plan_item else None,
        "known_limitations": [
            "no strategy is promoted for real-money use",
            "execution readiness remains false and no broker gateway exists",
            "manual tracking lacks corporate actions and external cash flows",
            "anns_d termination-announcement coverage is unavailable",
        ],
    }
    fingerprint = hashlib.sha256(
        json.dumps(core, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    payload = {**core, "acceptance_fingerprint": fingerprint}
    out_dir = output_root / fingerprint
    json_path = out_dir / "acceptance.json"
    html_path = out_dir / "acceptance.html"
    rows = "".join(
        f"<tr><td>{key}</td><td>{'PASS' if value else 'FAIL'}</td></tr>"
        for key, value in checks.items()
    )
    html = (
        "<!doctype html><meta charset='utf-8'><title>QuantLab Daily v1 Acceptance</title>"
        "<h1>QuantLab Daily v1 Acceptance</h1>"
        f"<p>product_ready={payload['product_ready']}</p><table>{rows}</table>"
        "<p>No strategy promotion or broker submission is claimed.</p>"
    )
    _atomic_write_text(html_path, html)
    _atomic_write_text(
        json_path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    return json_path, html_path, payload
