"""Conservative local observation-time admission for Forward Shadow evidence.

The 16:00-to-midnight window is a research admission policy, not an exchange
session rule or proof of data-provider completeness. Local clock provenance is
not a trusted timestamp service. Legacy artifacts have no bound timestamp.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from quantlab.data.models import DataValidationError

PREDICTION_V1 = "quantlab_forward_shadow_prediction_v1"
PREDICTION_V2 = "quantlab_forward_shadow_prediction_v2"
TIMING_POLICY = "same_signal_day_1600_to_midnight_shanghai_v1"
SHANGHAI = ZoneInfo("Asia/Shanghai")


def aware_timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise DataValidationError(f"forward-shadow {field} must be an aware ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise DataValidationError(f"forward-shadow {field} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DataValidationError(f"forward-shadow {field} must be timezone-aware")
    return parsed


def temporal_admission(signal_date: str, created_at: str, source_created_at: str) -> dict:
    """Classify observations without interpreting missing timing as admissible."""
    try:
        signal = date.fromisoformat(signal_date)
    except (TypeError, ValueError) as exc:
        raise DataValidationError("forward-shadow signal date is invalid") from exc
    created = aware_timestamp(created_at, "created_at")
    source = aware_timestamp(source_created_at, "source_daily_generated_at")
    if created < source:
        raise DataValidationError("forward-shadow prediction predates its Daily source")
    opens = datetime.combine(signal, time(16), SHANGHAI)
    closes = datetime.combine(signal + timedelta(days=1), time(), SHANGHAI)
    if created < opens or source < opens:
        status = "blocked_before_observation_window"
    elif created >= closes:
        status = "blocked_after_signal_day"
    else:
        status = "eligible_same_signal_day"
    return {
        "policy": TIMING_POLICY,
        "status": status,
        "forward_eligible": status == "eligible_same_signal_day",
    }


def prediction_timing(manifest: dict) -> dict:
    """Validate declared timing and return derived admission, including legacy."""
    schema = manifest.get("schema")
    if schema == PREDICTION_V1:
        # Even an apparently early timestamp was excluded from the v1 hash.
        return {
            "policy": "legacy_unbound_timestamp",
            "status": "blocked_legacy_unverified_time",
            "forward_eligible": False,
        }
    if schema != PREDICTION_V2:
        raise DataValidationError("unsupported forward-shadow prediction schema")
    label = manifest.get("label")
    horizon = label.get("horizon_sessions") if isinstance(label, dict) else None
    if isinstance(horizon, bool) or not isinstance(horizon, int) or horizon <= 0:
        raise DataValidationError("forward-shadow label horizon must be a positive integer")
    expected = temporal_admission(
        manifest.get("trade_date"),
        manifest.get("created_at"),
        manifest.get("source_daily_generated_at"),
    )
    declared = manifest.get("temporal_admission")
    if (
        not isinstance(declared, dict)
        or not isinstance(declared.get("forward_eligible"), bool)
        or declared != expected
    ):
        raise DataValidationError("forward-shadow temporal admission mismatch")
    digest = manifest.get("daily_report_sha256")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(char not in "0123456789abcdef" for char in digest)
    ):
        raise DataValidationError("forward-shadow Daily report hash is invalid")
    return expected
