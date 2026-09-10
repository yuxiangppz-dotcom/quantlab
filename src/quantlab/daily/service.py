"""Public Daily service facade with immutable content-addressed materialization.

The calculation implementation remains isolated in ``_service_impl`` so this
facade can stage a complete snapshot before publishing it. Published Daily
bundles are content addressed and never overwritten in place.
"""

from __future__ import annotations

import tempfile
from datetime import date, datetime
from pathlib import Path

from quantlab.daily import _service_impl as _impl
from quantlab.daily.materialization import commit_staged_daily_snapshot
from quantlab.data.models import DataValidationError

# Preserve the existing service surface, including a few private helpers used by
# internal modules. ``generate_daily_snapshot`` is replaced below with the
# immutable publishing facade.
for _name in dir(_impl):
    if not _name.startswith("__") and _name != "generate_daily_snapshot":
        globals()[_name] = getattr(_impl, _name)


def generate_daily_snapshot(
    requested_as_of: date | None = None,
    *,
    storage=None,
    config_path: Path = _impl.DEFAULT_CONFIG_PATH,
    product_root: Path = _impl.DEFAULT_PRODUCT_ROOT,
    now: datetime | None = None,
):
    """Generate into an isolated staging root, then publish immutably.

    A repeated build with identical content reuses the existing content-addressed
    bundle. If data, model inputs, or output-producing code change, a new sibling
    bundle is created and the prior bundle remains untouched.

    The calculation layer may retain native Python values in its returned report
    while ``report.json`` necessarily contains their JSON representation. Reload
    the staged snapshot through the normal disk loader before integrity checks so
    publication validates the exact bytes that will become evidence rather than
    an equivalent-but-not-identical in-memory representation.
    """
    product_root = Path(product_root)
    with tempfile.TemporaryDirectory(prefix="quantlab-daily-stage-") as temp_dir:
        stage_root = Path(temp_dir)
        _impl.generate_daily_snapshot(
            requested_as_of,
            storage=storage,
            config_path=config_path,
            product_root=stage_root,
            now=now,
        )
        staged = _impl.load_latest_snapshot(stage_root)
        if staged is None:
            raise DataValidationError("Daily staging completed without an active snapshot")
        return commit_staged_daily_snapshot(staged, product_root)
