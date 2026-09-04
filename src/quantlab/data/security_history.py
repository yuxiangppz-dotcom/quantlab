"""Security code change history loading."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from quantlab.data.models import SecurityCodeChange


def load_security_code_changes(path: str | Path) -> list[SecurityCodeChange]:
    """Load the security code change reference CSV into a list of changes."""
    frame = pd.read_csv(path)
    return [
        SecurityCodeChange(
            old_instrument_id=str(row["old_instrument_id"]),
            new_instrument_id=str(row["new_instrument_id"]),
            effective_date=date.fromisoformat(str(row["effective_date"])),
            old_name=str(row["old_name"]),
            original_list_date=date.fromisoformat(str(row["original_list_date"])),
        )
        for row in frame.to_dict("records")
    ]
