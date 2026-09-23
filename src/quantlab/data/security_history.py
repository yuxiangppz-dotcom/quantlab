"""Security code change history loading."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from quantlab.data.models import DataValidationError, SecurityCodeChange

_REQUIRED_COLUMNS = [
    "old_instrument_id",
    "new_instrument_id",
    "effective_date",
    "old_name",
    "original_list_date",
]


def load_security_code_changes(path: str | Path) -> list[SecurityCodeChange]:
    """Load and validate the security code change reference CSV."""
    frame = pd.read_csv(path)

    missing = [col for col in _REQUIRED_COLUMNS if col not in frame.columns]
    if missing:
        raise DataValidationError(f"missing required columns in code history: {missing}")

    changes: list[SecurityCodeChange] = []
    seen_old: set[str] = set()
    for row in frame.to_dict("records"):
        old_raw = row["old_instrument_id"]
        new_raw = row["new_instrument_id"]
        if pd.isna(old_raw) or pd.isna(new_raw):
            raise DataValidationError(f"empty instrument_id in code history: {row}")
        old = str(old_raw).strip()
        new = str(new_raw).strip()
        if not old or not new:
            raise DataValidationError(f"empty instrument_id in code history: {row}")
        if old == new:
            raise DataValidationError(f"old_instrument_id == new_instrument_id: {old}")
        if old in seen_old:
            raise DataValidationError(f"duplicate old_instrument_id: {old}")
        seen_old.add(old)

        try:
            effective = date.fromisoformat(str(row["effective_date"]))
            original = date.fromisoformat(str(row["original_list_date"]))
        except ValueError as exc:
            raise DataValidationError(f"invalid date in code history row: {row}") from exc
        if effective <= original:
            raise DataValidationError(f"effective_date <= original_list_date for {old}")

        changes.append(
            SecurityCodeChange(
                old_instrument_id=old,
                new_instrument_id=new,
                effective_date=effective,
                old_name=str(row["old_name"]),
                original_list_date=original,
            )
        )
    return changes


def code_at_observation_date(
    instrument_id: str, observed: date, changes: list[SecurityCodeChange]
) -> str:
    """Undo a vendor's retrospective successor code before its effective date."""
    for change in changes:
        if instrument_id == change.new_instrument_id and observed < change.effective_date:
            if observed < change.original_list_date:
                raise DataValidationError(
                    f"code predates original listing:{instrument_id}:{observed}"
                )
            return change.old_instrument_id
    return instrument_id
