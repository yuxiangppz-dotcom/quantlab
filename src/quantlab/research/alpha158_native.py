"""Pinned native Qlib Alpha158 expressions over explicit QuantLab input mappings.

Call native evaluators in an isolated worker: Qlib has process-global providers.
No provider API, model fitting, labels, or financial execution lives here.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import re
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd

from quantlab.daily.service import PROJECT_ROOT
from quantlab.data.models import DataValidationError
from quantlab.research.input_audit import _sha

CONTRACT_PATH = "config/qlib_alpha158_audit_v1.json"
CONTRACT_SHA = "df295e7a33607f2433c2cea735c08c2e65db04b855ef88aa6de838f0b7388d3f"
KEYS = ["instrument_id", "trade_date"]
PRICE_FIELDS = ["open", "high", "low", "close"]
FIELDS = [*PRICE_FIELDS, "volume", "vwap"]
RAW_FIELDS = [*PRICE_FIELDS, "volume", "amount", "adj_factor"]


def load_contract(root: Path = PROJECT_ROOT):
    raw = (root / CONTRACT_PATH).read_bytes()
    if hashlib.sha256(raw).hexdigest() != CONTRACT_SHA:
        raise DataValidationError("predeclared Alpha158 contract changed")
    return json.loads(raw)


def verify_library(contract):
    import qlib
    from qlib.contrib.data.loader import Alpha158DL
    from qlib.data.data import LocalExpressionProvider
    from qlib.data.ops import register_all_ops

    if importlib.metadata.version("pyqlib") != contract["qlib_version"]:
        raise DataValidationError("Qlib version differs from pinned contract")
    package = Path(qlib.__file__).parent
    for name, expected in contract["upstream_sources_sha256"].items():
        if _sha(package / name) != expected:
            raise DataValidationError(f"pinned Qlib source changed: {name}")
    register_all_ops(SimpleNamespace(custom_ops=None))
    provider = LocalExpressionProvider()
    expressions, names = Alpha158DL.get_feature_config()
    actual = []
    for name, expression in zip(names, expressions, strict=True):
        left, right = provider.get_expression_instance(expression).get_extended_window_size()
        actual.append(
            {
                "name": name,
                "expression": expression,
                "dependencies": sorted(set(re.findall(r"\$([a-z]+)", expression))),
                "lookback_sessions": int(left),
                "future_sessions": int(right),
            }
        )
    if (
        actual != contract["features"]
        or len(actual) != 158
        or any(x["future_sessions"] for x in actual)
    ):
        raise DataValidationError("native Alpha158 specification mismatch or future reference")
    # Compiled operators and all package source are also bound for this actual run.
    return {
        f.relative_to(package).as_posix(): _sha(f)
        for f in sorted(package.rglob("*"))
        if f.is_file() and f.suffix in (".py", ".so")
    }


def map_inputs(frame, sessions, instruments, list_dates, delist_dates):
    """Retain each code/calendar identity, including inactive and missing observations."""
    calendar = pd.DatetimeIndex(sessions)
    if (
        calendar.empty
        or calendar.has_duplicates
        or calendar.hasnans
        or calendar.tz is not None
        or not calendar.is_monotonic_increasing
        or not calendar.equals(calendar.normalize())
    ):
        raise DataValidationError("invalid native feature calendar")
    if not instruments or len(instruments) != len(set(instruments)):
        raise DataValidationError("instrument identities must be nonempty and unique")
    if any(re.fullmatch(r"[0-9]{6}\.(SH|SZ)", code) is None for code in instruments):
        raise DataValidationError("native audit requires exact SH/SZ stock codes")
    if not set(instruments).issubset(list_dates):
        raise DataValidationError("unknown native audit lifecycle")
    raw = frame[[*KEYS, *RAW_FIELDS]].copy()
    raw["trade_date"] = pd.to_datetime(raw.trade_date)
    if raw[KEYS].isna().any().any() or raw.duplicated(KEYS).any():
        raise DataValidationError("duplicate or null native input identities")
    if not raw.trade_date.isin(calendar).all() or not raw.instrument_id.isin(instruments).all():
        raise DataValidationError("native input escaped fixed calendar/instruments")
    grid = pd.MultiIndex.from_product([instruments, calendar], names=KEYS)
    present = raw.set_index(KEYS).index
    evidence = raw.set_index(KEYS).reindex(grid).reset_index()
    evidence["daily_observed"] = grid.isin(present)
    start = pd.to_datetime(evidence.instrument_id.map(list_dates))
    end = pd.to_datetime(evidence.instrument_id.map(delist_dates))
    active = evidence.trade_date.ge(start) & (end.isna() | evidence.trade_date.le(end))
    values = evidence[RAW_FIELDS].apply(pd.to_numeric, errors="coerce")
    price_ok = np.isfinite(values[PRICE_FIELDS]).all(axis=1) & values[PRICE_FIELDS].gt(0).all(
        axis=1
    )
    price_ok &= values.high.ge(values[["open", "close", "low"]].max(axis=1))
    price_ok &= values.low.le(values[["open", "close", "high"]].min(axis=1))
    factor_ok = np.isfinite(values.adj_factor) & values.adj_factor.gt(0)
    volume_ok = np.isfinite(values.volume) & values.volume.gt(0)
    amount_ok = np.isfinite(values.amount) & values.amount.gt(0)
    evidence["lifecycle_active"] = active
    evidence["price_inputs_valid"] = price_ok & factor_ok & active
    evidence["volume_inputs_valid"] = volume_ok & factor_ok & active & price_ok
    evidence["derived_raw_vwap"] = values.amount / values.volume
    vwap_in_bar = evidence.derived_raw_vwap.between(values.low, values.high)
    evidence["vwap_inputs_valid"] = evidence.volume_inputs_valid & amount_ok & vwap_in_bar
    evidence["exclusion_reason"] = np.select(
        [
            ~active,
            ~evidence.daily_observed,
            ~factor_ok,
            ~price_ok,
            ~volume_ok,
            ~amount_ok,
            ~vwap_in_bar,
        ],
        [
            "inactive_code_or_lifecycle",
            "missing_daily",
            "invalid_or_missing_factor",
            "invalid_ohlc",
            "nonpositive_or_missing_volume",
            "nonpositive_or_missing_amount",
            "derived_vwap_outside_bar",
        ],
        default="mapped_inputs_available",
    )
    mapped = evidence[KEYS].copy()
    for field in PRICE_FIELDS:
        mapped[field] = (values[field] * values.adj_factor).where(evidence.price_inputs_valid)
    mapped["volume"] = (values.volume / values.adj_factor).where(evidence.volume_inputs_valid)
    mapped["vwap"] = (values.amount / values.volume * values.adj_factor).where(
        evidence.vwap_inputs_valid
    )
    for field in FIELDS:
        mapped[field] = mapped[field].astype("float32")
        mapped[field] = mapped[field].where(np.isfinite(mapped[field]))
    return mapped, evidence


def write_binary_cache(mapped, folder, sessions):
    folder.mkdir(parents=True, exist_ok=False)
    calendar = pd.DatetimeIndex(sessions)
    (folder / "calendars").mkdir()
    (folder / "calendars/day.txt").write_text("\n".join(calendar.strftime("%Y-%m-%d")) + "\n")
    (folder / "instruments").mkdir()
    instruments = sorted(mapped.instrument_id.unique())
    (folder / "instruments/all.txt").write_text(
        "".join(f"{code}\t{calendar[0].date()}\t{calendar[-1].date()}\n" for code in instruments)
    )
    for code, data in mapped.groupby("instrument_id", sort=True):
        if re.fullmatch(r"[0-9]{6}\.(SH|SZ)", code) is None:
            raise DataValidationError("unsafe native instrument identity")
        data = data.set_index("trade_date").reindex(calendar)
        target = folder / "features" / code.lower()
        target.mkdir(parents=True)
        for field in FIELDS:
            np.r_[np.float32(0), data[field].to_numpy(dtype="float32")].astype("<f4").tofile(
                target / f"{field}.day.bin"
            )


def evaluate_native(mapped, folder, contract, *, memory=False):
    """Execute the pinned upstream expressions. Qlib global state is reset per call."""
    import qlib
    from qlib.data import D
    from qlib.data.data import FeatureD, FeatureProvider

    verify_library(contract)
    qlib.init(
        provider_uri=str(folder),
        kernels=1,
        expression_cache=None,
        dataset_cache=None,
        logging_level=40,
        local_cache_path=str(folder / "cache"),
        exp_manager={
            "class": "MLflowExpManager",
            "module_path": "qlib.workflow.expm",
            "kwargs": {
                "uri": (folder / "mlruns").resolve().as_uri(),
                "default_exp_name": "alpha158_parity_no_training",
            },
        },
    )
    if memory:
        frames = {
            code: data.sort_values("trade_date").reset_index(drop=True)[FIELDS]
            for code, data in mapped.groupby("instrument_id")
        }

        class InMemoryFields(FeatureProvider):
            def feature(self, instrument, field, start_index, end_index, freq):
                if freq != "day" or field[1:] not in FIELDS:
                    raise DataValidationError("unexpected native field query")
                return frames[instrument.upper()][field[1:]].loc[max(0, start_index) : end_index]

        FeatureD.register(InMemoryFields())
    expressions = [item["expression"] for item in contract["features"]]
    result = D.features(
        sorted(mapped.instrument_id.unique()),
        expressions,
        mapped.trade_date.min(),
        mapped.trade_date.max(),
    )
    result.columns = [item["name"] for item in contract["features"]]
    result = result.reset_index().rename(
        columns={"instrument": "instrument_id", "datetime": "trade_date"}
    )
    return result.sort_values(KEYS).reset_index(drop=True)


def apply_evidence_mask(native, mapped, contract):
    """Keep raw native output separately; missing history must not become evidence."""
    ordered = mapped.sort_values(KEYS).reset_index(drop=True)
    if not native[KEYS].equals(ordered[KEYS]):
        # Calendar precision is representation, not a missing-key substitution.
        left, right = native[KEYS].copy(), ordered[KEYS].copy()
        left["trade_date"], right["trade_date"] = (
            pd.to_datetime(left.trade_date),
            pd.to_datetime(right.trade_date),
        )
        if not left.astype(str).equals(right.astype(str)):
            raise DataValidationError("native expression output changed input membership")
    usable, coverage = native.copy(), []
    for item in contract["features"]:
        valid = np.isfinite(ordered[item["dependencies"]]).all(axis=1)
        window = item["lookback_sessions"] + 1
        complete = valid.groupby(ordered.instrument_id).transform(
            lambda values, window=window: (
                values.rolling(window, min_periods=window).sum().eq(window)
            )
        )
        finite = np.isfinite(native[item["name"]])
        usable[item["name"]] = native[item["name"]].where(complete & finite)
        coverage.append(
            {
                "name": item["name"],
                "rows": len(native),
                "complete_input_rows": int(complete.sum()),
                "native_nonfinite_rows": int((~finite).sum()),
                "usable_rows": int((complete & finite).sum()),
                "incomplete_but_native_finite_rows": int((~complete & finite).sum()),
            }
        )
    return usable, coverage


def compare_native(primary, reference, contract):
    if not primary[KEYS].astype(str).equals(reference[KEYS].astype(str)):
        raise DataValidationError("native provider parity membership mismatch")
    comparisons = []
    for item in contract["features"]:
        a, b = primary[item["name"]].to_numpy(), reference[item["name"]].to_numpy()
        same = (a == b) | (np.isnan(a) & np.isnan(b))
        finite = np.isfinite(a) & np.isfinite(b)
        comparisons.append(
            {
                "name": item["name"],
                "mismatch_rows": int((~same).sum()),
                "max_absolute_difference": float(np.abs(a[finite] - b[finite]).max())
                if finite.any()
                else None,
                "status": "matched" if same.all() else "mismatch",
            }
        )
    return comparisons
