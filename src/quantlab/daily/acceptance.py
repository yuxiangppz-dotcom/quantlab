"""Compact end-to-end acceptance artifact for the local Daily v1 product."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pandas as pd

from quantlab.daily.experiments import load_baseline_view, load_latest_factor_view
from quantlab.daily.integrity import load_validated_latest_snapshot
from quantlab.daily.service import (
    PROJECT_ROOT,
    _atomic_write_text,
    inspect_data_status,
)
from quantlab.personal import (
    list_accounts,
    load_effective_account,
    load_latest_plan,
    manual_tracking_fixture_smoke,
)

DEFAULT_ACCEPTANCE_ROOT = PROJECT_ROOT / "data" / "products" / "acceptance"


def _git(command: str) -> str:
    return subprocess.check_output(["git", *command.split()], cwd=PROJECT_ROOT, text=True).strip()


def run_v1_acceptance(
    account_id: str = "demo_200k", *, output_root: Path = DEFAULT_ACCEPTANCE_ROOT
) -> tuple[Path, Path, dict]:
    status = inspect_data_status()
    snapshot = load_validated_latest_snapshot()
    baseline = load_baseline_view()
    factor_view = load_latest_factor_view()
    accounts = list_accounts()
    account = None
    plan_item = None
    plan_binding_error = None
    if account_id in accounts:
        try:
            account = load_effective_account(account_id)
            plan_item = load_latest_plan(
                account_id, account_fingerprint=account["account_fingerprint"]
            )
        except (FileNotFoundError, KeyError, ValueError) as exc:
            plan_binding_error = str(exc)
    plan = plan_item[1] if plan_item else None
    plan_account_bound = bool(
        plan and account and plan.get("account_fingerprint") == account.get("account_fingerprint")
    )
    plan_daily_bound = bool(
        plan
        and snapshot
        and plan.get("daily_content_fingerprint") == snapshot.report.get("content_fingerprint")
    )
    plan_csv_bound = bool(plan and plan.get("csv_sha256"))
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
        "bounded_factor_research_available": factor_view is not None,
        "account_available": account_id in accounts,
        "reference_plan_available": plan_item is not None,
        "reference_plan_current_account_bound": plan_account_bound,
        "reference_plan_current_daily_bound": plan_daily_bound,
        "reference_plan_csv_fingerprint_verified": plan_csv_bound,
        "manual_tracking_fixture_smoke": manual_tracking_fixture_smoke(),
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
        "version": "1.2.0",
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
        "factor_research": (
            {"run_id": factor_view["run_id"], "signal_end": factor_view["signal_end"]}
            if factor_view
            else None
        ),
        "account_id": account_id,
        "plan_id": plan_item[1]["plan_id"] if plan_item else None,
        "plan_binding_error": plan_binding_error,
        "known_limitations": [
            "no strategy is promoted for real-money use",
            "execution readiness remains false and no broker gateway exists",
            "manual tracking lacks corporate-action postings "
            "and full historical/performance readiness",
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
