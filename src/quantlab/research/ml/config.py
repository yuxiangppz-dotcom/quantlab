"""Small, explicit search space and one clock for labels, fitting and execution."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class MLConfig:
    schema: str = "quantlab_ml_v2"
    decision_hour: int = 16
    horizon_sessions: int = 10
    execution_lag: int = 1
    price_basis: str = "next_session_adjusted_close"
    train_sessions: int = 882
    validation_sessions: int = 126
    min_cross_section: int = 20
    min_feature_fraction: float = 0.8
    models: tuple[str, ...] = ("ridge", "lightgbm")
    label_transform: str = "rank"
    learning_rate: float = 0.03
    num_leaves: int = 15
    max_depth: int = 4
    min_data_in_leaf: int = 1000
    feature_fraction: float = 0.8
    bagging_fraction: float = 0.8
    lambda_l2: float = 10.0
    max_rounds: int = 1500
    early_stopping_rounds: int = 100
    ridge_alpha: float = 10.0
    seed: int = 20260916
    num_threads: int = 2
    max_matrix_bytes: int = 8_000_000_000
    rebalance_sessions: int = 5
    max_positions: int = 20
    entry_rank: int = 30
    exit_rank: int = 60
    max_replacements: int = 5
    min_hold_sessions: int = 10
    gross_exposure: float = 0.8
    max_weight: float = 0.05
    max_industry_weight: float = 0.2
    max_one_way_turnover: float = 0.25
    min_trade_fen: int = 300_000
    execution_weight_tolerance: float = 0.005

    def __post_init__(self):
        if self.schema != "quantlab_ml_v2":
            raise ValueError("unsupported ML schema")
        if self.execution_lag != 1 or self.price_basis != "next_session_adjusted_close":
            raise ValueError("v2 supports only next-session close, matching quantity scheduler")
        if type(self.decision_hour) is not int or not 15 <= self.decision_hour <= 23:
            raise ValueError("decision_hour must be an integer in [15, 23] Shanghai time")
        positive = (
            "horizon_sessions",
            "train_sessions",
            "validation_sessions",
            "min_cross_section",
            "num_leaves",
            "max_depth",
            "min_data_in_leaf",
            "max_rounds",
            "early_stopping_rounds",
            "num_threads",
            "max_matrix_bytes",
            "rebalance_sessions",
            "max_positions",
            "entry_rank",
            "exit_rank",
            "max_replacements",
        )
        for key in positive:
            if type(getattr(self, key)) is not int or getattr(self, key) <= 0:
                raise ValueError(f"{key} must be a positive integer")
        for key in ("min_hold_sessions", "min_trade_fen", "seed"):
            if type(getattr(self, key)) is not int or getattr(self, key) < 0:
                raise ValueError(f"{key} must be a nonnegative integer")
        for key in (
            "min_feature_fraction",
            "learning_rate",
            "feature_fraction",
            "bagging_fraction",
            "gross_exposure",
            "max_weight",
            "max_industry_weight",
            "max_one_way_turnover",
            "execution_weight_tolerance",
        ):
            value = getattr(self, key)
            if isinstance(value, bool) or not math.isfinite(value) or not 0 < value <= 1:
                raise ValueError(f"{key} must be a finite fraction in (0, 1]")
        for key in ("lambda_l2", "ridge_alpha"):
            if not math.isfinite(getattr(self, key)) or getattr(self, key) <= 0:
                raise ValueError(f"{key} must be positive")
        if not self.max_positions <= self.entry_rank < self.exit_rank:
            raise ValueError("require max_positions <= entry_rank < exit_rank")
        if self.gross_exposure / self.max_positions > self.max_weight:
            raise ValueError("equal slot weight exceeds single-name cap")
        if self.max_replacements > self.max_positions:
            raise ValueError("replacement budget exceeds position count")
        if min(self.train_sessions, self.validation_sessions) <= self.horizon_sessions + 1:
            raise ValueError("training and validation windows must exceed label maturity lag")
        if self.label_transform not in {"rank", "zscore"}:
            raise ValueError("label_transform must be rank or zscore")
        if not self.models or len(set(self.models)) != len(self.models):
            raise ValueError("models must be nonempty and unique")
        if set(self.models) - {"ridge", "lightgbm", "binary", "lambdarank"}:
            raise ValueError("unsupported model")

    def payload(self):
        return asdict(self)

    @property
    def fingerprint(self):
        return hashlib.sha256(json.dumps(self.payload(), sort_keys=True).encode()).hexdigest()


def load_config(path) -> MLConfig:
    from pathlib import Path as _Path

    payload = json.loads(_Path(path).read_text(encoding="utf-8"))
    if "models" in payload:
        payload["models"] = tuple(payload["models"])
    return MLConfig(**payload)
