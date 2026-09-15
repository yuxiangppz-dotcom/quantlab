"""Read-only source adapter for the commissioned Alpha158 scenario."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext
from functools import lru_cache

import pandas as pd

from quantlab.data import ParquetStorage
from quantlab.research.model_replay_accounting import Distribution, ReplayEvidenceError, fen
from quantlab.research.model_replay_intake import bound_scores, rank_targets, raw_dividend_receipt
from quantlab.research.quantity_kernel import (
    ResearchFeeScenario,
    ResearchQuantityRules,
    ResearchSession,
)
from quantlab.research.round2_dataset import sealed_read
from quantlab.research.rule_evidence_v2 import load_catalogue


def canonical_integer(value, scale=1):
    """Recover only the binary-float ULP error in a canonical CNY amount."""
    with localcontext() as ctx:
        ctx.prec = 70
        decimal = Decimal.from_float(float(value)) * scale
        closest = decimal.to_integral_value()
        tolerance = Decimal.from_float(math.ulp(float(value))) * scale
        if not decimal.is_finite() or abs(decimal - closest) > tolerance:
            raise ReplayEvidenceError(f"amount has material sub-fen precision:{value}")
        return int(closest)


def canonical_amount_fen(value):
    return canonical_integer(value, 100)


def price_bound_fen(value, *, upper):
    """Intersect the provider's decimal interval with the integer-fen price grid.

    This converts bounds, NOT transaction prices. In particular 999999.999 is
    retained as a very wide provider interval, not turned into a traded quote.
    """
    bound = Decimal(str(value)) * 100
    if not bound.is_finite() or bound <= 0:
        raise ReplayEvidenceError("invalid provider price bound")
    return int(bound.to_integral_value(rounding=ROUND_FLOOR if upper else ROUND_CEILING))


class ReplayData:
    def __init__(self, root):
        self.root, self.canonical, self.manifest = root, root / "data/canonical", {}
        self.rule_book = load_catalogue(root)
        for name in ("v1", "v2"):
            path = root / f"config/historical_rule_sources_{name}.json"
            self.bind(path)
            for relative in json.loads(path.read_text())["files"]:
                self.bind(root / relative)
        store = ParquetStorage(self.canonical)
        for kind in ("calendar", "securities"):
            for path in (self.canonical / kind).rglob("*.parquet"):
                self.bind(path)
        self.calendar = tuple(
            sorted({x.trade_date for x in store.load_trading_calendar() if x.is_open})
        )
        self.securities = {x.instrument_id: x for x in store.load_securities()}
        changes = root / "config/security_code_changes.csv"
        self.bind(changes)
        self.changes = pd.read_csv(changes).to_dict("records")
        self.partition = lru_cache(maxsize=100)(self._partition)
        self.events = {}
        self.unplaced = {}

    def bind(self, path):
        raw = path.read_bytes()
        entry = {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
        key = str(path.resolve())
        if key in self.manifest and self.manifest[key] != entry:
            raise ReplayEvidenceError(f"source_changed:{key}")
        self.manifest[key] = entry
        return entry

    def targets(self, exclude_st=False):
        model = self.root / "data/products/alpha158_rolling/alpha158_rolling_20260911"
        self.bind(model / "report.json")
        report = sealed_read(model / "report.json")
        attempts = {r["slot"]: r for r in report["attempts"]}
        frames = []
        for fold, phase in (
            ("fold1", "evaluation"),
            ("fold2", "evaluation"),
            ("fold3", "evaluation"),
            ("fold3", "observed_2026"),
        ):
            slot = fold + "_lightgbm"
            path = model / "fits" / slot / f"scores_{phase}.parquet"
            self.bind(path)
            frames.append(bound_scores(path, attempts[slot]["artifacts"][path.name]))
        scores = pd.concat(frames)
        if exclude_st:
            kept = []
            for day, group in scores.groupby("trade_date", sort=True):
                excluded = set(
                    self.partition("lifecycle_context_v1/stock_st", pd.Timestamp(day).date())
                )
                kept.append(group.loc[~group.instrument_id.isin(excluded)])
            scores = pd.concat(kept)
        return rank_targets(scores, pd.DatetimeIndex(self.calendar))

    def _partition(self, kind, day):
        path = self.canonical / kind / f"year={day.year}/month={day.month:02d}/{day}.parquet"
        if not path.exists():
            raise ReplayEvidenceError(f"partition_missing:{kind}:{day}")
        self.bind(path)
        frame = pd.read_parquet(path)
        if not frame.empty and not (pd.to_datetime(frame.trade_date).dt.date == day).all():
            raise ReplayEvidenceError(f"partition_date_mismatch:{kind}:{day}")
        if frame.instrument_id.duplicated().any():
            if kind == "lifecycle_context_v1/suspensions":
                return {
                    code: {"records": group.to_dict("records")}
                    for code, group in frame.groupby("instrument_id")
                }
            raise ReplayEvidenceError(f"duplicate_instrument:{kind}:{day}")
        return frame.set_index("instrument_id").to_dict("index")

    def suspended(self, code, day):
        row = self.partition("lifecycle_context_v1/suspensions", day).get(code)
        if not row:
            return False
        rows = row.get("records", [row])
        return all(r["suspend_type"] == "S" and pd.isna(r["suspend_timing"]) for r in rows)

    def security(self, code, day):
        security = self.securities.get(code)
        if security:
            if not security.list_date <= day or (
                security.delist_date and day >= security.delist_date
            ):
                raise ReplayEvidenceError(f"outside_listed_interval:{code}:{day}")
            return security.exchange, {
                "主板": "MAIN",
                "中小板": "MAIN",
                "创业板": "CHINEXT",
                "科创板": "STAR",
            }[security.board]
        for change in self.changes:
            if change["old_instrument_id"] == code and (
                change["original_list_date"] <= str(day) < change["effective_date"]
            ):
                successor = self.securities[change["new_instrument_id"]]
                return successor.exchange, {"创业板": "CHINEXT", "主板": "MAIN"}[successor.board]
        raise ReplayEvidenceError(f"historical_identity_unknown:{code}:{day}")

    def rules(self, code, day):
        rule = self.rule_book.resolve(*self.security(code, day), day)
        if rule is None:
            raise ReplayEvidenceError(f"quantity_rule_unknown:{code}:{day}")
        return ResearchQuantityRules(
            rule.rule_id,
            rule.effective_from,
            rule.effective_to,
            rule.buy_min_quantity,
            rule.buy_quantity_step,
            rule.sell_min_quantity,
            rule.sell_quantity_step,
            rule.max_limit_quantity,
            rule.allow_full_odd_lot_exit,
        )

    def context(self, code, day, signal):
        position = self.calendar.index(day)
        following = self.calendar[position + 1]
        fees = ResearchFeeScenario(
            "user_full_commission_086_min5_stamp_separate_slip5bp",
            day,
            day,
            Decimal("0.000086"),
            500,
            Decimal(0),
            Decimal("0.001") if day < date(2023, 8, 28) else Decimal("0.0005"),
            Decimal(0),
            0,
            Decimal("0.0005"),
        )
        if self.suspended(code, day):
            return ResearchSession(
                code,
                day,
                following,
                day,
                True,
                False,
                True,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                Decimal("0.05"),
                None,
                fees,
            )
        bar = self.partition("daily", day).get(code)
        limit = self.partition("daily_price_limit", day).get(code)
        if bar is None or limit is None:
            raise ReplayEvidenceError(f"execution_bar_or_limits_missing:{code}:{day}")
        amounts = []
        previous = self.calendar.index(signal)
        for prior in self.calendar[previous - 19 : previous + 1]:
            row = self.partition("daily", prior).get(code)
            if row is not None:
                amounts.append(canonical_amount_fen(row["amount"]))
            elif self.suspended(code, prior):
                amounts.append(0)
            else:
                raise ReplayEvidenceError(f"prior20_activity_unknown:{code}:{prior}")
        if len(amounts) != 20:
            raise ReplayEvidenceError("prior20_incomplete")
        volume = canonical_integer(bar["volume"])
        return ResearchSession(
            code,
            day,
            following,
            day,
            True,
            True,
            True,
            fen(bar["close"]),
            fen(bar["low"]),
            fen(bar["high"]),
            price_bound_fen(limit["down_limit"], upper=False),
            price_bound_fen(limit["up_limit"], upper=True),
            sum(amounts) // 20,
            signal,
            20,
            canonical_amount_fen(bar["amount"]),
            int(volume),
            Decimal("0.05"),
            self.rules(code, day),
            fees,
        )

    def load_events(self, codes, start, end):
        folder = (
            self.root / "data/products/corporate_action_staging/dividend_targets_20260911/attempts"
        )
        for n, code in enumerate(sorted(codes), 1):
            code_folder = folder / code
            if not code_folder.exists():
                code_folder = (
                    self.root
                    / "data/products/model_replay/exst_dividend_supplement_v1"
                    / "attempts"
                    / code
                )
            rows, sources = raw_dividend_receipt(code_folder, code)
            for source in sources:
                self.bind(source)
            for row in rows:
                if row["div_proc"] != "实施":
                    continue
                record = row.get("record_date")
                if not record:
                    if row.get("ex_date") and str(start).replace("-", "") <= row["ex_date"] <= str(
                        end
                    ).replace("-", ""):
                        self.unplaced.setdefault(code, []).append(row)
                    continue
                day = datetime.strptime(record, "%Y%m%d").date()
                if start <= day <= end:
                    key = (code, day)
                    self.events.setdefault(key, []).append(row)
            if n % 1000 == 0:
                print(f"corporate source instruments: {n}", flush=True)

    def distributions(self, code, day):
        rows = self.events.get((code, day), [])
        if not rows:
            return []
        # Duplicate provenance rows do not create duplicate economic entitlements.
        fields = (
            "record_date",
            "ex_date",
            "pay_date",
            "div_listdate",
            "cash_div_tax",
            "stk_div",
            "stk_bo_rate",
            "stk_co_rate",
        )
        unique = {tuple(row[k] for k in fields): row for row in rows}
        if len(unique) != 1:
            raise ReplayEvidenceError(f"conflicting_distribution:{code}:{day}")
        row = next(iter(unique.values()))

        def number(key):
            value = row[key]
            if value is None:
                raise ReplayEvidenceError(f"distribution_{key}_unknown:{code}:{day}")
            value = Decimal(str(value))
            if not value.is_finite() or value < 0:
                raise ReplayEvidenceError(f"invalid_distribution_{key}:{code}:{day}")
            return value

        def when(key):
            return datetime.strptime(row[key], "%Y%m%d").date() if row[key] else None

        cash, stock = number("cash_div_tax"), number("stk_div")
        ex, pay, listing = when("ex_date"), when("pay_date"), when("div_listdate")
        if ex is None or ex <= day or (cash and (pay is None or pay < ex)):
            raise ReplayEvidenceError(f"distribution_dates_unknown_or_invalid:{code}:{day}")
        if stock:
            if row["stk_bo_rate"] is not None:
                bonus = number("stk_bo_rate")
            elif row["stk_co_rate"] is not None:
                bonus = stock - number("stk_co_rate")
            else:
                raise ReplayEvidenceError(f"stock_tax_breakdown_unknown:{code}:{day}")
            if not 0 <= bonus <= stock:
                raise ReplayEvidenceError(f"stock_ratio_inconsistent:{code}:{day}")
            # Conversion provenance must be verified before excluding it from taxable dividends.
            if stock != bonus:
                raise ReplayEvidenceError(f"conversion_tax_source_required:{code}:{day}")
        else:
            bonus = Decimal(0)
        return [Distribution(f"{code}:{day}", code, day, ex, pay, listing, cash, stock, bonus)]
