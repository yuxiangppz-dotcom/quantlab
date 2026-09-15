"""Deterministic Chinese review artifact for a sealed S5-B diagnostic."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from quantlab.data.models import canonical_payload_fingerprint
from quantlab.research.s5_base_completion import S5BaseState
from quantlab.research.s5_base_diagnostic_metrics import (
    S5BaseDiagnosticMetrics,
    S5BaseDistributionRow,
    S5BaseSpreadRow,
    S5BaseSummaryStats,
)
from quantlab.research.s5_base_diagnostic_protocol import (
    frozen_s5_base_diagnostic_protocol,
)
from quantlab.research.s5_base_diagnostic_run_seal import S5BaseDiagnosticRunSeal

_SCHEMA = "quantlab_s5b_diagnostic_review_v1"
_REVIEW_STATUS = "awaiting_explicit_user_review"


@dataclass(frozen=True)
class S5BaseDiagnosticReviewArtifact:
    schema: str
    strategy_id: str
    protocol_fingerprint: str
    input_fingerprint: str
    metrics_fingerprint: str
    seal_fingerprint: str
    recorded_at: datetime
    review_status: str
    markdown_zh: str
    content_fingerprint: str = field(init=False)
    diagnostic_only: bool = True
    executable_pnl: bool = False
    holding_policy_frozen: bool = False
    performance_verdict: bool = False
    promotion_authority: bool = False
    account_mutation_authority: bool = False
    broker_order_authority: bool = False
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        protocol = frozen_s5_base_diagnostic_protocol()
        if self.schema != _SCHEMA:
            raise ValueError(f"schema must equal {_SCHEMA}")
        if self.strategy_id != protocol.strategy_id:
            raise ValueError("strategy_id must equal the frozen S5-B strategy")
        if self.protocol_fingerprint != protocol.fingerprint:
            raise ValueError("protocol_fingerprint must equal the frozen protocol")
        for name in (
            "input_fingerprint",
            "metrics_fingerprint",
            "seal_fingerprint",
            "review_status",
            "markdown_zh",
        ):
            value = getattr(self, name)
            if not value or not value.strip():
                raise ValueError(f"{name} must be non-empty")
        if self.review_status != _REVIEW_STATUS:
            raise ValueError("review artifact cannot imply an automatic verdict")
        if (
            not self.diagnostic_only
            or self.executable_pnl
            or self.holding_policy_frozen
            or self.performance_verdict
            or self.promotion_authority
            or self.account_mutation_authority
            or self.broker_order_authority
        ):
            raise ValueError("review artifact cannot acquire performance or execution authority")
        object.__setattr__(
            self,
            "content_fingerprint",
            canonical_payload_fingerprint({"markdown_zh": self.markdown_zh}),
        )
        object.__setattr__(
            self,
            "fingerprint",
            canonical_payload_fingerprint(_artifact_payload(self)),
        )


def build_s5_base_diagnostic_review(
    *,
    metrics: S5BaseDiagnosticMetrics,
    seal: S5BaseDiagnosticRunSeal,
) -> S5BaseDiagnosticReviewArtifact:
    """Validate a completed seal and render a claim-neutral Chinese review."""

    protocol = frozen_s5_base_diagnostic_protocol()
    if metrics.protocol_fingerprint != protocol.fingerprint:
        raise ValueError("metrics must bind the frozen S5-B protocol")
    if seal.protocol_fingerprint != protocol.fingerprint:
        raise ValueError("seal must bind the frozen S5-B protocol")
    if seal.strategy_id != protocol.strategy_id:
        raise ValueError("seal strategy does not match the frozen S5-B strategy")
    if seal.input_fingerprint != metrics.input_fingerprint:
        raise ValueError("seal and metrics input fingerprints do not match")
    if seal.metrics_fingerprint != metrics.fingerprint:
        raise ValueError("seal does not bind the supplied metrics")
    if seal.review_status != _REVIEW_STATUS:
        raise ValueError("seal is not awaiting explicit user review")
    if (
        not metrics.diagnostic_only
        or metrics.executable_pnl
        or metrics.holding_policy_frozen
        or metrics.performance_claim
        or metrics.broker_order_authority
        or not seal.diagnostic_only
        or seal.holding_policy_frozen
        or seal.performance_claim
        or seal.promotion_authority
        or seal.account_mutation_authority
        or seal.broker_order_authority
    ):
        raise ValueError("source evidence acquired forbidden authority")

    return S5BaseDiagnosticReviewArtifact(
        schema=_SCHEMA,
        strategy_id=protocol.strategy_id,
        protocol_fingerprint=protocol.fingerprint,
        input_fingerprint=metrics.input_fingerprint,
        metrics_fingerprint=metrics.fingerprint,
        seal_fingerprint=seal.fingerprint,
        recorded_at=seal.recorded_at,
        review_status=_REVIEW_STATUS,
        markdown_zh=_render_markdown(metrics, seal),
    )


def _render_markdown(
    metrics: S5BaseDiagnosticMetrics,
    seal: S5BaseDiagnosticRunSeal,
) -> str:
    lines = [
        "# S5-B 筑底完成信号：冻结历史诊断审阅",
        "",
        "> 这是历史 close-to-close 标签诊断，不是可执行 PnL、买卖建议或策略通过结论。",
        "> 1/5/10/20 日仅是观察窗口，不是最低持有期；后续仍可独立选择持有 1 日。",
        "",
        "## 证据身份",
        "",
        "| 字段 | 值 |",
        "|---|---|",
        f"| strategy | {_cell(seal.strategy_id)} |",
        f"| recorded_at_utc | {_cell(seal.recorded_at.isoformat())} |",
        f"| protocol_fingerprint | {_cell(metrics.protocol_fingerprint)} |",
        f"| input_fingerprint | {_cell(metrics.input_fingerprint)} |",
        f"| metrics_fingerprint | {_cell(metrics.fingerprint)} |",
        f"| seal_fingerprint | {_cell(seal.fingerprint)} |",
        f"| review_status | {_cell(seal.review_status)} |",
        "",
        "## 状态数量与比例",
        "",
        "| 状态 | 数量 | 比例 |",
        "|---|---:|---:|",
    ]
    state_order = {state: index for index, state in enumerate(S5BaseState)}
    for row in sorted(metrics.state_counts, key=lambda item: state_order[item.state]):
        lines.append(f"| {_cell(row.state.value)} | {row.count} | {_number(row.rate)} |")

    lines += [
        "",
        "## 相邻已观测信号日的状态迁移",
        "",
        "这些迁移不表示相邻交易日，也不证明中间日期持续处于同一状态。",
        "",
        "| 起始状态 | 下一观测状态 | 数量 | 条件比例 |",
        "|---|---|---:|---:|",
    ]
    transitions = sorted(
        metrics.state_transitions,
        key=lambda row: (state_order[row.from_state], state_order[row.to_state]),
    )
    if not transitions:
        lines.append("| N/A | N/A | 0 | N/A |")
    for row in transitions:
        lines.append(
            f"| {_cell(row.from_state.value)} | {_cell(row.to_state.value)} "
            f"| {row.count} | {_number(row.conditional_rate)} |"
        )

    lines += [
        "",
        "## 每日研究目标与现金",
        "",
        "| 信号日 | 入选数 | 研究目标暴露 | 现金 |",
        "|---|---:|---:|---:|",
    ]
    for row in sorted(metrics.allocations, key=lambda item: item.as_of):
        lines.append(
            f"| {row.as_of.isoformat()} | {row.selected_n} "
            f"| {_number(row.research_target_exposure)} "
            f"| {_number(row.cash_exposure)} |"
        )

    lines += ["", "## 各状态未来标签分布", ""]
    lines += _distribution_table(metrics.state_distributions, include_period=False)
    lines += ["", "## 状态与入选组日期配对差值", ""]
    lines += _spread_table(metrics.cohort_spreads)
    lines += ["", "## 与冻结对照的日期配对差值", ""]
    lines += _spread_table(metrics.comparison_spreads)
    lines += ["", "## 入选信号月度分布", ""]
    lines += _distribution_table(
        metrics.monthly_selected_distributions,
        include_period=True,
    )
    lines += ["", "## 入选信号宽基准环境分布", ""]
    lines += _distribution_table(
        metrics.regime_selected_distributions,
        include_period=False,
    )
    lines += [
        "",
        "## 审阅边界",
        "",
        "- 当前状态仅为 awaiting_explicit_user_review；本报告不自动给出通过或失败。",
        "- 空组保留为观测数 0、统计值 N/A，不能填零或静默删除。",
        "- 所有 spread 均先在信号日内等权，再让有效信号日等权。",
        "- 本报告不冻结持有期，不授予绩效、晋级、账户变更或券商下单权限。",
        "",
    ]
    return "\n".join(lines)


def _distribution_table(
    rows: tuple[S5BaseDistributionRow, ...],
    *,
    include_period: bool,
) -> list[str]:
    if include_period:
        result = [
            "| 分组 | 期间 | 窗口 | 观测数 | 均值 | 中位数 | P10 | 最差值 | 正收益率 |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    else:
        result = [
            "| 分组 | 窗口 | 观测数 | 均值 | 中位数 | P10 | 最差值 | 正收益率 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    for row in sorted(rows, key=lambda item: (item.period_id, item.group_id, item.horizon)):
        cells = " | ".join(_stats_cells(row.stats))
        if include_period:
            result.append(
                f"| {_cell(row.group_id)} | {_cell(row.period_id)} "
                f"| {row.horizon} | {cells} |"
            )
        else:
            result.append(f"| {_cell(row.group_id)} | {row.horizon} | {cells} |")
    return result


def _spread_table(rows: tuple[S5BaseSpreadRow, ...]) -> list[str]:
    result = [
        "| 差值 | 窗口 | 有效日期数 | 均值 | 中位数 | P10 | 最差值 | 正差值率 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in sorted(rows, key=lambda item: (item.spread_id, item.horizon)):
        result.append(
            f"| {_cell(row.spread_id)} | {row.horizon} "
            f"| {' | '.join(_stats_cells(row.stats))} |"
        )
    return result


def _stats_cells(stats: S5BaseSummaryStats) -> list[str]:
    return [
        str(stats.observation_count),
        _number(stats.mean),
        _number(stats.median),
        _number(stats.p10),
        _number(stats.minimum),
        _number(stats.strict_positive_rate),
    ]


def _number(value: float | None) -> str:
    return "N/A" if value is None else format(value, ".8f")


def _cell(value: str) -> str:
    return value.replace("\n", " ").replace("|", "\\|")


def _artifact_payload(
    artifact: S5BaseDiagnosticReviewArtifact,
) -> dict[str, object]:
    payload = {
        name: (
            getattr(artifact, name).isoformat()
            if isinstance(getattr(artifact, name), datetime)
            else getattr(artifact, name)
        )
        for name in artifact.__dataclass_fields__
        if name not in {"content_fingerprint", "fingerprint"}
    }
    payload["content_fingerprint"] = artifact.content_fingerprint
    return payload
