"""V1 research universe filter."""

from __future__ import annotations

import pandas as pd


def is_v1_a_share(instrument_id: str) -> bool:
    """True for Shanghai/Shenzhen A-shares; False for BJ and B-shares.

    Public form of the historical universe predicate so other layers (e.g. the
    benchmark control portfolio) reuse the exact same V1 definition instead of
    re-implementing it.
    """
    return _is_v1_a_share(instrument_id)


def _is_v1_a_share(instrument_id: str) -> bool:
    """True for Shanghai/Shenzhen A-shares; False for BJ and B-shares."""
    if instrument_id.endswith(".BJ"):
        return False
    if instrument_id.endswith(".SH"):
        return not instrument_id.startswith("900")
    if instrument_id.endswith(".SZ"):
        return not instrument_id.startswith("200")
    return False


def filter_v1_universe(frame: pd.DataFrame) -> pd.DataFrame:
    """Keep only Shanghai/Shenzhen A-shares (exclude BJ and B-shares)."""
    mask = frame["instrument_id"].map(_is_v1_a_share)
    return frame[mask].reset_index(drop=True)
