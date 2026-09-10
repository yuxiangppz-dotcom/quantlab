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
from quantlab.data.enrichment import inspect_enrichment_status
from quantlab.data.models import DataValidationError
from quantlab.data.storage import ParquetStorage
from quantlab.data.sync import (
    audit_daily_adj_coverage,
    validate_adj_factors,
    validate_daily_bars,
    validate_daily_basic,
)
from quantlab.portfolio.product import (
    construct_daily_fixed_count_portfolio,
    fixed_count_config_from_daily,
)
from quantlab.research import build_research_dataset, filter_v1_universe
from quantlab.research.factor_registry import (
    FACTOR_REGISTRY,
    add_transparent_combination,
    build_factor_columns,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "daily_mvp_v1.json"
DEFAULT_PRODUCT_ROOT = PROJECT_ROOT / "data" / "products" / "daily"
CORE_DATASETS = ("daily", "adj_factor", "daily_basic")
SUPPORTED_BOARDS = ("主板", "创业板", "科创板")


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
        day for day, flags in calendar_by_date.items() if day <= requested and any(flags)
    )
    latest_expected = open_dates[-1] if open_dates else None

    if requested > latest_calendar_date:
        requested_session_status = "unknown_calendar_out_of_coverage"
        issues.append(
            f"calendar coverage ends at {latest_calendar_date}; sessions through "
            f"{requested} are unknown"
        )
    else:
        requested_session_status = "open" if any(calendar_by_date.get(requested, [])) else "closed"

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
        "latest_expected_open_session": (latest_expected.isoformat() if latest_expected else None),
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
    supported_scores = {
        "return_20d": "lower_is_better",
        "transparent_combo_v1": "higher_is_better",
    }
    if config["score_definition"] not in supported_scores:
        raise DataValidationError("unsupported daily score_definition")
    if config["score_direction"] != supported_scores[config["score_definition"]]:
        raise DataValidationError("score direction does not match its registered definition")
    boards = config.setdefault("allowed_boards", list(SUPPORTED_BOARDS))
    if (
        not isinstance(boards, list)
        or not boards
        or len(boards) != len(set(boards))
        or not set(boards).issubset(SUPPORTED_BOARDS)
    ):
        raise DataValidationError("allowed_boards must be a non-empty supported subset")
    count = config["target_count"]
    cap = config["max_weight_per_name"]
    gross = config["gross_exposure"]
    if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
        raise DataValidationError("target_count must be a positive integer")
    if not all(isinstance(value, int | float) and math.isfinite(value) for value in (cap, gross)):
        raise DataValidationError("weight values must be finite numbers")
    if not (0 < cap <= 1 and 0 < gross <= 1):
        raise DataValidationError("weight values must be in (0, 1]")
    # The product adapter is the economic contract. Validating it here prevents
    # the Daily generator from accepting a config that downstream portfolio
    # construction would reject (most importantly, a different tie policy).
    fixed_count_config_from_daily(config)
    return config


def _apply_portfolio_contract(
    ranking: pd.DataFrame,
    effective: date,
    config: dict,
) -> tuple[pd.DataFrame, int, int, float, float]:
    """Materialize Daily ranks while delegating economic targets to Portfolio Core."""
    # Validate even when the cross-section is empty; an empty day must not let an
    # invalid product contract slip through merely because there are no names.
    fixed_count_config_from_daily(config)
    ascending = config["score_direction"] == "lower_is_better"
    materialized = ranking.sort_values(
        ["alpha_score", "instrument_id"],
        ascending=[ascending, True],
        na_position="last",
        kind="mergesort",
    ).reset_index(drop=True)
    valid_count = int(materialized["alpha_score"].notna().sum())
    materialized["rank"] = pd.array(
        list(range(1, valid_count + 1)) + [pd.NA] * (len(materialized) - valid_count),
        dtype="Int64",
    )

    if materialized.empty:
        materialized["selected"] = pd.Series(dtype=bool)
        materialized["target_weight"] = pd.Series(dtype=float)
        return materialized, 0, 0, 0.0, 1.0

    portfolio = construct_daily_fixed_count_portfolio(
        materialized[["instrument_id", "trade_date", "alpha_score"]],
        effective,
        config,
    )
    weights = {item.instrument_id: item.target_weight for item in portfolio.positions}
    materialized["selected"] = materialized["instrument_id"].isin(weights)
    materialized["target_weight"] = materialized["instrument_id"].map(weights).fillna(0.0)
    selected_count = len(portfolio.positions)
    per_name = portfolio.positions[0].target_weight if portfolio.positions else 0.0
    return materialized, valid_count, selected_count, per_name, portfolio.cash_weight


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
    price_limit_loaded = storage.daily_price_limit_exists(trade_date)
    price_limit_map = (
        {item.instrument_id: item for item in storage.load_daily_price_limits_by_date(trade_date)}
        if price_limit_loaded
        else {}
    )
    if price_limit_loaded and set(ranking["instrument_id"]) - set(price_limit_map):
        raise DataValidationError("loaded stk_limit partition does not cover the Daily ranking")
    close_map = dict(zip(ranking["instrument_id"], ranking["close"], strict=True))
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
        if not price_limit_loaded:
            parts.append("PRICE_LIMIT_CONTEXT_NOT_LOADED")
        else:
            limit = price_limit_map[instrument_id]
            close = float(close_map[instrument_id])
            if round(close, 4) == round(limit.up_limit, 4):
                parts.append("CLOSE_AT_REPORTED_UP_LIMIT")
            elif round(close, 4) == round(limit.down_limit, 4):
                parts.append("CLOSE_AT_REPORTED_DOWN_LIMIT")
            elif limit.down_limit <= close <= limit.up_limit:
                parts.append("CLOSE_WITHIN_REPORTED_LIMITS_NOT_NEXT_SESSION_EVIDENCE")
            else:
                parts.append("CLOSE_OUTSIDE_REPORTED_LIMITS_REQUIRES_REVIEW")
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

    basics = pd.DataFrame([asdict(item) for item in storage.load_daily_basic_by_date(effective)])
    ranking = ranking.merge(
        basics[["instrument_id", "turnover_rate", "total_mv", "circ_mv"]],
        on="instrument_id",
        how="left",
        validate="one_to_one",
    )
    raw = pd.DataFrame([asdict(item) for item in storage.load_daily_bars_by_date(effective)])
    ranking = ranking.merge(
        raw[["instrument_id", "open", "high", "low", "amount"]],
        on="instrument_id",
        validate="one_to_one",
    )
    securities = {item.instrument_id: item for item in storage.load_securities()}
    ranking["name"] = ranking["instrument_id"].map(
        lambda value: securities[value].name if value in securities else None
    )
    ranking["board"] = ranking["instrument_id"].map(
        lambda value: securities[value].board if value in securities else None
    )
    ranking = ranking[ranking["board"].isin(config["allowed_boards"])].copy()
    ranking = build_factor_columns(ranking)
    ranking = add_transparent_combination(
        ranking,
        ["reversal_20d", "low_amplitude", "small_size", "intraday_strength"],
    )
    ranking["alpha_score"] = ranking[config["score_definition"]]
    ranking, valid_count, selected_count, per_name, cash_weight = _apply_portfolio_contract(
        ranking,
        effective,
        config,
    )
    ranking["selection_reason"] = ranking.apply(
        lambda row: (
            f"RESEARCH_TARGET: {config['score_definition']} / "
            f"{config['score_direction']} under {config['model_status']}"
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
    if storage.daily_price_limit_exists(effective):
        limits = pd.DataFrame(
            [asdict(item) for item in storage.load_daily_price_limits_by_date(effective)]
        )
        ranking = ranking.merge(
            limits[["instrument_id", "up_limit", "down_limit"]],
            on="instrument_id",
            how="left",
            validate="one_to_one",
        )
        ranking["price_limit_data_status"] = "provider_reported_for_signal_date"
    else:
        ranking["up_limit"] = float("nan")
        ranking["down_limit"] = float("nan")
        ranking["price_limit_data_status"] = "not_loaded"
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
            *[item.factor_id for item in FACTOR_REGISTRY],
            "reversal_20d_combo_contribution",
            "low_amplitude_combo_contribution",
            "small_size_combo_contribution",
            "intraday_strength_combo_contribution",
            "transparent_combo_v1",
            "selected",
            "target_weight",
            "selection_reason",
            "risk_context",
            "up_limit",
            "down_limit",
            "price_limit_data_status",
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
            "up_limit",
            "down_limit",
            "price_limit_data_status",
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
    if storage.daily_price_limit_exists(effective):
        input_paths["stk_limit"] = storage.daily_price_limit_path(effective)
    input_fingerprints = {name: _sha256_file(path) for name, path in input_paths.items()}
    code_paths = {
        "daily_service": Path(__file__),
        "momentum_alpha": PROJECT_ROOT / "src" / "quantlab" / "alpha" / "momentum.py",
        "research_dataset": PROJECT_ROOT / "src" / "quantlab" / "research" / "dataset.py",
        "factor_registry": PROJECT_ROOT / "src" / "quantlab" / "research" / "factor_registry.py",
        "universe": PROJECT_ROOT / "src" / "quantlab" / "research" / "universe.py",
        "portfolio_product": PROJECT_ROOT / "src" / "quantlab" / "portfolio" / "product.py",
        "portfolio_constructor": (
            PROJECT_ROOT / "src" / "quantlab" / "portfolio" / "constructor.py"
        ),
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
        "enrichment": inspect_enrichment_status(storage, effective),
        "ranking": {
            "universe_rows": len(ranking),
            "valid_score_rows": valid_count,
            "selected_rows": selected_count,
            "tie_policy": config["tie_policy"],
            "score_interpretation": (
                f"{config['score_definition']} is ranked {config['score_direction']}; "
                f"model status is {config['model_status']} and this is not a profit claim"
            ),
            "factor_detail_columns": [item.factor_id for item in FACTOR_REGISTRY]
            + ["transparent_combo_v1"],
            "factor_contribution_columns": [
                "reversal_20d_combo_contribution",
                "low_amplitude_combo_contribution",
                "small_size_combo_contribution",
                "intraday_strength_combo_contribution",
            ],
            "factor_selection_note": (
                "baseline selects frozen return_20d reversal; transparent_combo_v1 is an "
                "unpromoted, test-observed candidate with retrospective portfolio audit"
            ),
        },
        "target": {
            "status": "research_target_only",
            "position_weight": per_name,
            "position_weight_sum": target_weight_sum,
            "cash_weight": round(float(cash_weight), 12),
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
            "signal-date stk_limit is context only; next-session limits remain unknown",
            "financial observations are prospective-only because revision timestamps are absent",
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
            key: value for key, value in report_core["data_status"].items() if key != "inspected_at"
        },
    }
    content_fingerprint = _canonical_hash(
        {
            "report": fingerprint_report,
            "ranking_sha256": hashlib.sha256(ranking_text.encode()).hexdigest(),
            "target_sha256": hashlib.sha256(target_text.encode()).hexdigest(),
        }
    )
    config_version = f"{config['config_id']}-{input_fingerprints['config'][:12]}"
    out_dir = product_root / effective.isoformat() / config_version
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
            _activate_snapshot(product_root, report_path, content_fingerprint)
            return DailySnapshot(report_path, ranking_path, target_path, html_path, existing, True)

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
    _activate_snapshot(product_root, report_path, content_fingerprint)
    return DailySnapshot(report_path, ranking_path, target_path, html_path, report, False)


def _activate_snapshot(product_root: Path, report_path: Path, fingerprint: str) -> None:
    pointer = {
        "schema": "quantlab_daily_active_v1",
        "report_path": str(report_path.relative_to(product_root)),
        "content_fingerprint": fingerprint,
    }
    _atomic_write_text(
        product_root / "ACTIVE.json",
        json.dumps(pointer, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def load_latest_snapshot(product_root: Path = DEFAULT_PRODUCT_ROOT) -> DailySnapshot | None:
    """Load the newest committed local daily snapshot without recomputing it."""
    if not product_root.exists():
        return None
    active_path = product_root / "ACTIVE.json"
    if active_path.exists():
        pointer = json.loads(active_path.read_text(encoding="utf-8"))
        report_path = (product_root / pointer["report_path"]).resolve()
        if not report_path.is_relative_to(product_root.resolve()):
            raise ValueError("active daily pointer escapes the product root")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if report.get("content_fingerprint") != pointer.get("content_fingerprint"):
            raise ValueError("active daily pointer fingerprint mismatch")
        out_dir = report_path.parent
        return DailySnapshot(
            report_path=report_path,
            ranking_path=out_dir / "ranking.csv",
            target_path=out_dir / "target_portfolio.csv",
            html_path=out_dir / "report.html",
            report=report,
            reused=True,
        )
    candidates = list(product_root.glob("**/report.json"))
    if not candidates:
        return None
    loaded = [(path, json.loads(path.read_text(encoding="utf-8"))) for path in candidates]
    report_path, report = max(
        loaded,
        key=lambda item: (
            item[1]["effective_as_of"],
            item[1].get("generated_at", ""),
            str(item[0]),
        ),
    )
    out_dir = report_path.parent
    return DailySnapshot(
        report_path=out_dir / "report.json",
        ranking_path=out_dir / "ranking.csv",
        target_path=out_dir / "target_portfolio.csv",
        html_path=out_dir / "report.html",
        report=report,
        reused=True,
    )