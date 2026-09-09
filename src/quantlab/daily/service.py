"""Read-only daily ranking workflow over QuantLab canonical data.

The workflow deliberately does not call a provider.  Data synchronization is
an explicit command, so a UI refresh can never download data or mutate an
account.  A snapshot is always labelled with the common complete data date;
an older cache is never presented as today's close.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from quantlab.alpha import calculate_momentum_alpha
from quantlab.data.models import DataValidationError
from quantlab.data.storage import ParquetStorage
from quantlab.data.sync import (
    audit_daily_adj_coverage,
    validate_adj_factors,
    validate_daily_bars,
    validate_daily_basic,
)
from quantlab.research import build_research_dataset, filter_v1_universe

SHANGHAI = ZoneInfo("Asia/Shanghai")
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "daily_mvp_v1.json"
DEFAULT_PRODUCT_ROOT = PROJECT_ROOT / "data" / "products" / "daily"
CORE_DATASETS = ("daily", "adj_factor", "daily_basic")


@dataclass(frozen=True)
class DailySnapshot:
    """Paths and summary for one materialized daily snapshot."""

    report_path: Path
    ranking_path: Path
    target_path: Path
    html_path: Path
    report: dict
    reused: bool


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_hash(payload: object) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def _git_head() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT, text=True
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return None


def _git_worktree_clean() -> bool | None:
    try:
        output = subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=PROJECT_ROOT, text=True
        )
        return not output.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def _dataset_exists(storage: ParquetStorage, dataset: str, trade_date: date) -> bool:
    if dataset == "daily":
        return storage.daily_bars_exists(trade_date)
    if dataset == "adj_factor":
        return storage.adj_factor_exists(trade_date)
    if dataset == "daily_basic":
        return storage.daily_basic_exists(trade_date)
    if dataset == "index_daily":
        return storage.index_daily_exists(trade_date)
    raise ValueError(f"unsupported dataset: {dataset}")


def _latest_existing_date(
    storage: ParquetStorage, dataset: str, open_dates: list[date]
) -> date | None:
    return next(
        (day for day in reversed(open_dates) if _dataset_exists(storage, dataset, day)),
        None,
    )


def inspect_data_status(
    storage: ParquetStorage | None = None,
    requested_as_of: date | None = None,
    *,
    now: datetime | None = None,
) -> dict:
    """Inspect local data and select the latest common complete model date.

    File presence is checked across all required datasets first.  Only the
    newest common candidate is opened and validated, keeping the scan bounded.
    If that candidate is corrupt, the function fails closed instead of silently
    falling back to an older result.
    """
    storage = storage or ParquetStorage(PROJECT_ROOT / "data" / "canonical")
    local_now = now or datetime.now(SHANGHAI)
    if local_now.tzinfo is None or local_now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    requested = requested_as_of or local_now.astimezone(SHANGHAI).date()
    calendar = storage.load_trading_calendar()
    issues: list[str] = []
    if not calendar:
        return {
            "requested_as_of": requested.isoformat(),
            "inspected_at": local_now.astimezone(SHANGHAI).isoformat(),
            "status": "blocked_no_calendar",
            "effective_as_of": None,
            "latest_calendar_date": None,
            "latest_expected_open_session": None,
            "requested_session_status": "unknown",
            "stale_open_sessions": None,
            "datasets": {},
            "issues": ["trading calendar is missing or empty"],
        }

    latest_calendar_date = max(entry.trade_date for entry in calendar)
    calendar_by_date: dict[date, list[bool]] = {}
    for entry in calendar:
        calendar_by_date.setdefault(entry.trade_date, []).append(entry.is_open)
    open_dates = sorted(
        day
        for day, flags in calendar_by_date.items()
        if day <= requested and any(flags)
    )
    latest_expected = open_dates[-1] if open_dates else None

    if requested > latest_calendar_date:
        requested_session_status = "unknown_calendar_out_of_coverage"
        issues.append(
            f"calendar coverage ends at {latest_calendar_date}; sessions through "
            f"{requested} are unknown"
        )
    else:
        requested_session_status = (
            "open" if any(calendar_by_date.get(requested, [])) else "closed"
        )

    datasets: dict[str, dict] = {}
    for name in (*CORE_DATASETS, "index_daily"):
        latest = _latest_existing_date(storage, name, open_dates)
        datasets[name] = {
            "required_by_daily_model": name in CORE_DATASETS,
            "latest_date": latest.isoformat() if latest else None,
        }

    effective = next(
        (
            day
            for day in reversed(open_dates)
            if all(_dataset_exists(storage, name, day) for name in CORE_DATASETS)
        ),
        None,
    )
    row_counts: dict[str, int] = {}
    coverage: dict[str, object] = {}
    if effective is not None:
        bars = storage.load_daily_bars_by_date(effective)
        factors = storage.load_adj_factors_by_date(effective)
        basics = storage.load_daily_basic_by_date(effective)
        validate_daily_bars(bars, effective)
        validate_adj_factors(factors, effective)
        validate_daily_basic(basics, effective)
        audit = audit_daily_adj_coverage(storage, effective)
        if audit.daily_missing_adj_count:
            raise DataValidationError(
                f"{effective} has {audit.daily_missing_adj_count} daily rows without adj_factor"
            )
        daily_ids = {item.instrument_id for item in bars}
        basic_ids = {item.instrument_id for item in basics}
        if daily_ids != basic_ids:
            missing = sorted(daily_ids - basic_ids)[:5]
            extra = sorted(basic_ids - daily_ids)[:5]
            raise DataValidationError(
                f"{effective} daily/daily_basic universe mismatch: missing={missing}, extra={extra}"
            )
        row_counts = {
            "daily": len(bars),
            "adj_factor": len(factors),
            "daily_basic": len(basics),
            "index_daily": len(storage.load_index_daily_by_date(effective)),
        }
        coverage = asdict(audit)
        coverage["trade_date"] = effective.isoformat()
        for name, count in row_counts.items():
            datasets[name]["rows_on_effective_date"] = count

    if effective is None:
        status = "blocked_no_common_complete_date"
        stale_open_sessions = None
        issues.append("no open session has all required daily model datasets")
    else:
        stale_open_sessions = sum(day > effective for day in open_dates)
        if requested > latest_calendar_date:
            status = "stale_calendar_unknown"
            stale_open_sessions = None
        elif effective != latest_expected:
            status = "stale_missing_required_data"
            issues.append(
                f"latest expected open session is {latest_expected}, but common model data "
                f"ends at {effective}"
            )
        elif requested_session_status == "closed":
            status = "complete_previous_session_for_closed_date"
        else:
            status = "complete"

    return {
        "requested_as_of": requested.isoformat(),
        "inspected_at": local_now.astimezone(SHANGHAI).isoformat(),
        "status": status,
        "effective_as_of": effective.isoformat() if effective else None,
        "latest_calendar_date": latest_calendar_date.isoformat(),
        "latest_expected_open_session": (
            latest_expected.isoformat() if latest_expected else None
        ),
        "requested_session_status": requested_session_status,
        "stale_open_sessions": stale_open_sessions,
        "datasets": datasets,
        "coverage": coverage,
        "issues": issues,
    }


def _load_config(path: Path) -> dict:
    config = json.loads(path.read_text(encoding="utf-8"))
    required = {
        "config_id",
        "strategy_id",
        "model_status",
        "score_definition",
        "score_direction",
        "target_count",
        "max_weight_per_name",
        "gross_exposure",
        "tie_policy",
        "test_observed",
        "performance_claim",
    }
    missing = required - set(config)
    if missing:
        raise DataValidationError(f"daily config missing fields: {sorted(missing)}")
    if config["score_definition"] != "return_20d":
        raise DataValidationError("daily_mvp_v1 only supports score_definition=return_20d")
    if config["score_direction"] != "lower_is_better":
        raise DataValidationError("daily_mvp_v1 baseline direction must remain lower_is_better")
    count = config["target_count"]
    cap = config["max_weight_per_name"]
    gross = config["gross_exposure"]
    if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
        raise DataValidationError("target_count must be a positive integer")
    if not all(isinstance(value, int | float) and math.isfinite(value) for value in (cap, gross)):
        raise DataValidationError("weight values must be finite numbers")
    if not (0 < cap <= 1 and 0 < gross <= 1):
        raise DataValidationError("weight values must be in (0, 1]")
    return config


def _next_open_session(storage: ParquetStorage, current: date) -> date | None:
    candidates = sorted(
        {
            entry.trade_date
            for entry in storage.load_trading_calendar()
            if entry.is_open and entry.trade_date > current
        }
    )
    return candidates[0] if candidates else None


def _risk_context(storage: ParquetStorage, trade_date: date, ranking: pd.DataFrame) -> list[str]:
    st_loaded = storage.stock_st_v1_exists(trade_date)
    suspension_loaded = storage.suspensions_v1_exists(trade_date)
    st_map = (
        {item.instrument_id: item for item in storage.load_stock_st_v1_by_date(trade_date)}
        if st_loaded
        else {}
    )
    suspension_map: dict[str, list] = {}
    if suspension_loaded:
        for item in storage.load_suspensions_v1_by_date(trade_date):
            suspension_map.setdefault(item.instrument_id, []).append(item)

    labels: list[str] = []
    for instrument_id in ranking["instrument_id"]:
        parts: list[str] = []
        if not st_loaded:
            parts.append("ST_CONTEXT_NOT_LOADED")
        elif instrument_id in st_map:
            item = st_map[instrument_id]
            parts.append(f"ST_CONTEXT_REPORTED:{item.status or item.type_name or 'unspecified'}")
        else:
            parts.append("NO_ST_RECORD_NOT_SAFETY_EVIDENCE")
        if not suspension_loaded:
            parts.append("SUSPENSION_CONTEXT_NOT_LOADED")
        elif instrument_id in suspension_map:
            kinds = "+".join(sorted({item.suspend_type for item in suspension_map[instrument_id]}))
            parts.append(f"SUSPENSION_CONTEXT_REPORTED:{kinds}")
        else:
            parts.append("NO_SUSPENSION_RECORD_NOT_FILL_EVIDENCE")
        labels.append(";".join(parts))
    return labels


def _build_html(report: dict, ranking: pd.DataFrame, target: pd.DataFrame) -> str:
    title = f"QuantLab Daily — {report['effective_as_of']}"
    warning = "；".join(report["data_status"]["issues"]) or "本地必需数据完整"
    return "\n".join(
        [
            "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>",
            f"<title>{title}</title>",
            "<style>body{font-family:Arial,sans-serif;margin:2rem;max-width:1400px}",
            "table{border-collapse:collapse;width:100%}th,td{padding:.35rem;border:1px solid #ddd}",
            ".warning{background:#fff3cd;padding:1rem}</style></head><body>",
            f"<h1>{title}</h1>",
            f"<p class='warning'>状态：{report['data_status']['status']}。{warning}</p>",
            "<p>研究示例，不是投资建议；排名不等于可执行订单或成交。</p>",
            "<h2>目标组合（研究目标）</h2>",
            target.to_html(index=False, border=0),
            "<h2>股票排名</h2>",
            ranking.head(200).to_html(index=False, border=0),
            "</body></html>",
        ]
    )


def generate_daily_snapshot(
    requested_as_of: date | None = None,
    *,
    storage: ParquetStorage | None = None,
    config_path: Path = DEFAULT_CONFIG_PATH,
    product_root: Path = DEFAULT_PRODUCT_ROOT,
    now: datetime | None = None,
) -> DailySnapshot:
    """Generate or reuse a deterministic daily ranking snapshot."""
    storage = storage or ParquetStorage(PROJECT_ROOT / "data" / "canonical")
    local_now = now or datetime.now(SHANGHAI)
    status = inspect_data_status(storage, requested_as_of, now=local_now)
    if status["effective_as_of"] is None:
        raise DataValidationError("cannot generate daily snapshot without a complete data date")
    effective = date.fromisoformat(status["effective_as_of"])
    config = _load_config(config_path)

    dataset = build_research_dataset(
        storage,
        effective,
        effective,
        return_horizons=(1, 5, 20),
        forward_horizons=(),
    )
    universe = filter_v1_universe(dataset)
    alpha = calculate_momentum_alpha(universe, lookback=20)
    ranking = universe[
        ["instrument_id", "trade_date", "close", "return_1d", "return_5d", "return_20d"]
    ].merge(alpha, on=["instrument_id", "trade_date"], validate="one_to_one")

    basics = pd.DataFrame(
        [asdict(item) for item in storage.load_daily_basic_by_date(effective)]
    )
    ranking = ranking.merge(
        basics[["instrument_id", "turnover_rate", "total_mv", "circ_mv"]],
        on="instrument_id",
        how="left",
        validate="one_to_one",
    )
    securities = {item.instrument_id: item for item in storage.load_securities()}
    ranking["name"] = ranking["instrument_id"].map(
        lambda value: securities[value].name if value in securities else None
    )
    ranking["board"] = ranking["instrument_id"].map(
        lambda value: securities[value].board if value in securities else None
    )
    ranking = ranking.sort_values(
        ["alpha_score", "instrument_id"],
        ascending=[True, True],
        na_position="last",
        kind="mergesort",
    ).reset_index(drop=True)
    valid_count = int(ranking["alpha_score"].notna().sum())
    ranking["rank"] = pd.array(
        list(range(1, valid_count + 1)) + [pd.NA] * (len(ranking) - valid_count),
        dtype="Int64",
    )
    selected_count = min(config["target_count"], valid_count)
    ranking["selected"] = False
    if selected_count:
        ranking.loc[: selected_count - 1, "selected"] = True
    per_name = min(
        config["max_weight_per_name"],
        config["gross_exposure"] / selected_count if selected_count else 0.0,
    )
    ranking["target_weight"] = 0.0
    if selected_count:
        ranking.loc[: selected_count - 1, "target_weight"] = per_name
    ranking["selection_reason"] = ranking.apply(
        lambda row: (
            "RESEARCH_TARGET: lowest 20-session adjusted return under frozen example baseline"
            if row["selected"]
            else (
                "NO_TARGET: outside deterministic top target_count"
                if pd.notna(row["alpha_score"])
                else "NO_TARGET: insufficient exact-session lookback"
            )
        ),
        axis=1,
    )
    ranking["risk_context"] = _risk_context(storage, effective, ranking)
    ranking = ranking[
        [
            "rank",
            "instrument_id",
            "name",
            "board",
            "trade_date",
            "close",
            "alpha_score",
            "return_1d",
            "return_5d",
            "return_20d",
            "turnover_rate",
            "total_mv",
            "circ_mv",
            "selected",
            "target_weight",
            "selection_reason",
            "risk_context",
        ]
    ]
    target = ranking.loc[ranking["selected"]].copy()
    target = target[
        [
            "instrument_id",
            "name",
            "rank",
            "close",
            "alpha_score",
            "target_weight",
            "selection_reason",
            "risk_context",
        ]
    ]

    input_paths = {
        "daily": storage.daily_bars_path(effective),
        "adj_factor": storage.adj_factor_path(effective),
        "daily_basic": storage.daily_basic_path(effective),
        "securities": storage.securities_path,
        "calendar": storage.calendar_path,
        "config": config_path,
    }
    input_fingerprints = {
        name: _sha256_file(path) for name, path in input_paths.items()
    }
    code_paths = {
        "daily_service": Path(__file__),
        "momentum_alpha": PROJECT_ROOT / "src" / "quantlab" / "alpha" / "momentum.py",
        "research_dataset": PROJECT_ROOT / "src" / "quantlab" / "research" / "dataset.py",
        "universe": PROJECT_ROOT / "src" / "quantlab" / "research" / "universe.py",
    }
    next_session = _next_open_session(storage, effective)
    target_weight_sum = round(float(target["target_weight"].sum()), 12)
    report_core = {
        "schema": "quantlab_daily_v1",
        "requested_as_of": status["requested_as_of"],
        "effective_as_of": effective.isoformat(),
        "next_known_open_session": next_session.isoformat() if next_session else None,
        "data_status": status,
        "model": config,
        "ranking": {
            "universe_rows": len(ranking),
            "valid_score_rows": valid_count,
            "selected_rows": selected_count,
            "tie_policy": config["tie_policy"],
            "score_interpretation": (
                "lower return_20d ranks first because this frozen example baseline tests "
                "short-horizon reversal; it is test-observed and not a profit claim"
            ),
        },
        "target": {
            "status": "research_target_only",
            "position_weight": per_name,
            "position_weight_sum": target_weight_sum,
            "cash_weight": round(1.0 - target_weight_sum, 12),
            "next_session_review_required": True,
            "reason": (
                "no account state, next-session market status, executable quote, or verified "
                "fee authority is asserted by the daily ranking"
            ),
        },
        "provenance": {
            "code_head": _git_head(),
            "git_worktree_clean": _git_worktree_clean(),
            "source_sha256": {name: _sha256_file(path) for name, path in code_paths.items()},
            "input_sha256": input_fingerprints,
        },
        "claims": {
            "performance_claim": False,
            "broker_order": False,
            "fill_claim": False,
            "real_account_state": False,
        },
        "known_limitations": [
            "baseline results were already observed and do not establish future profitability",
            "absence of an ST or suspension context row is not proof of tradability",
            "a T-close research target is not a T+1 executable order or fill",
            "systematic termination-announcement coverage remains incomplete",
        ],
    }

    ranking_buffer = io.StringIO()
    ranking.to_csv(ranking_buffer, index=False, lineterminator="\n")
    ranking_text = ranking_buffer.getvalue()
    target_buffer = io.StringIO()
    target.to_csv(target_buffer, index=False, lineterminator="\n")
    target_text = target_buffer.getvalue()
    fingerprint_report = {
        **report_core,
        "data_status": {
            key: value
            for key, value in report_core["data_status"].items()
            if key != "inspected_at"
        },
    }
    content_fingerprint = _canonical_hash(
        {
            "report": fingerprint_report,
            "ranking_sha256": hashlib.sha256(ranking_text.encode()).hexdigest(),
            "target_sha256": hashlib.sha256(target_text.encode()).hexdigest(),
        }
    )
    out_dir = product_root / effective.isoformat()
    report_path = out_dir / "report.json"
    ranking_path = out_dir / "ranking.csv"
    target_path = out_dir / "target_portfolio.csv"
    html_path = out_dir / "report.html"
    outputs_exist = all(
        path.exists() for path in (report_path, ranking_path, target_path, html_path)
    )
    if outputs_exist:
        existing = json.loads(report_path.read_text(encoding="utf-8"))
        if existing.get("content_fingerprint") == content_fingerprint:
            return DailySnapshot(
                report_path, ranking_path, target_path, html_path, existing, True
            )

    report = {
        **report_core,
        "generated_at": local_now.astimezone(SHANGHAI).isoformat(),
        "content_fingerprint": content_fingerprint,
    }
    html = _build_html(report, ranking, target)
    _atomic_write_text(ranking_path, ranking_text)
    _atomic_write_text(target_path, target_text)
    _atomic_write_text(html_path, html)
    _atomic_write_text(
        report_path,
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    return DailySnapshot(report_path, ranking_path, target_path, html_path, report, False)


def load_latest_snapshot(product_root: Path = DEFAULT_PRODUCT_ROOT) -> DailySnapshot | None:
    """Load the newest committed local daily snapshot without recomputing it."""
    if not product_root.exists():
        return None
    candidates = sorted(
        path for path in product_root.iterdir() if path.is_dir() and (path / "report.json").exists()
    )
    if not candidates:
        return None
    out_dir = candidates[-1]
    report = json.loads((out_dir / "report.json").read_text(encoding="utf-8"))
    return DailySnapshot(
        report_path=out_dir / "report.json",
        ranking_path=out_dir / "ranking.csv",
        target_path=out_dir / "target_portfolio.csv",
        html_path=out_dir / "report.html",
        report=report,
        reused=True,
    )
