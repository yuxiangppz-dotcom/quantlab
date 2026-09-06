"""Point-in-time A-share rule book with authoritative-source provenance."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from urllib.parse import urlparse

from quantlab.execution.models import (
    ExecutionValidationError,
    OrderSession,
    OrderType,
    Side,
    require_decimal,
    require_identifier,
    require_int,
)


@dataclass(frozen=True)
class RuleSource:
    authority: str
    title: str
    url: str
    published_on: date
    retrieved_on: date
    document_sha256: str

    def __post_init__(self) -> None:
        require_identifier(self.authority, "authority")
        require_identifier(self.title, "source title")
        parsed = urlparse(self.url)
        if parsed.scheme != "https":
            raise ExecutionValidationError("rule source URL must use https")
        allowed_hosts = {
            "www.sse.com.cn",
            "big5.sse.com.cn",
            "edu.sse.com.cn",
            "www.szse.cn",
            "investor.szse.cn",
            "docs.static.szse.cn",
        }
        if parsed.hostname not in allowed_hosts:
            raise ExecutionValidationError(
                f"rule source is not an approved exchange host: {parsed.hostname}"
            )
        if len(self.document_sha256) != 64 or any(
            char not in "0123456789abcdef" for char in self.document_sha256
        ):
            raise ExecutionValidationError("document_sha256 must be lowercase SHA-256")
        if self.retrieved_on < self.published_on:
            raise ExecutionValidationError("rule source retrieved before publication")


@dataclass(frozen=True)
class AShareTradingRule:
    rule_id: str
    version: str
    exchange: str
    board: str
    effective_from: date
    effective_to: date
    sources: tuple[RuleSource, ...]
    supported_order_types: tuple[OrderType, ...]
    supported_sessions: tuple[OrderSession, ...]
    price_tick: Decimal
    buy_min_quantity: int
    buy_quantity_step: int
    sell_min_quantity: int
    sell_quantity_step: int
    max_limit_quantity: int
    allow_full_odd_lot_exit: bool
    sellability_lag_sessions: int
    limitations: tuple[str, ...]

    def __post_init__(self) -> None:
        require_identifier(self.rule_id, "rule_id")
        require_identifier(self.version, "version")
        require_identifier(self.exchange, "exchange")
        require_identifier(self.board, "board")
        if self.effective_to < self.effective_from:
            raise ExecutionValidationError("rule effective interval is reversed")
        if not self.sources:
            raise ExecutionValidationError("trading rule requires authoritative sources")
        if not any(source.published_on <= self.effective_from for source in self.sources):
            raise ExecutionValidationError(
                "rule has no authoritative source published by its effective date"
            )
        source_urls = [source.url for source in self.sources]
        if len(source_urls) != len(set(source_urls)):
            raise ExecutionValidationError("duplicate source URL in trading rule")
        if not self.supported_order_types or not self.supported_sessions:
            raise ExecutionValidationError("rule implementation scope cannot be empty")
        require_decimal(self.price_tick, "price_tick", positive=True)
        require_int(self.buy_min_quantity, "buy_min_quantity", minimum=1)
        require_int(self.buy_quantity_step, "buy_quantity_step", minimum=1)
        require_int(self.sell_min_quantity, "sell_min_quantity", minimum=1)
        require_int(self.sell_quantity_step, "sell_quantity_step", minimum=1)
        require_int(self.max_limit_quantity, "max_limit_quantity", minimum=1)
        if self.sellability_lag_sessions != 1:
            raise ExecutionValidationError(
                "v0.1 A-share rules require one-session sellability lag"
            )
        if not self.limitations:
            raise ExecutionValidationError("trading rule must disclose limitations")

    def applies_on(self, value: date) -> bool:
        return self.effective_from <= value <= self.effective_to

    def quantity_is_admissible(
        self,
        *,
        side: Side,
        quantity: int,
        total_position_quantity: int,
    ) -> bool:
        if quantity > self.max_limit_quantity:
            return False
        if not isinstance(side, Side):
            raise ExecutionValidationError("side must be a Side enum")
        if side is Side.BUY:
            return (
                quantity >= self.buy_min_quantity
                and (quantity - self.buy_min_quantity) % self.buy_quantity_step == 0
            )
        if quantity >= self.sell_min_quantity and (
            quantity - self.sell_min_quantity
        ) % self.sell_quantity_step == 0:
            return True
        return self.allow_full_odd_lot_exit and quantity == total_position_quantity


class PITRuleBook:
    """Resolve exactly one rule by exchange, board, and exchange-local date."""

    def __init__(self, rules: tuple[AShareTradingRule, ...]) -> None:
        self.rules = tuple(rules)
        self._by_scope: dict[tuple[str, str], tuple[AShareTradingRule, ...]] = {}
        ids = [rule.rule_id for rule in self.rules]
        if len(ids) != len(set(ids)):
            raise ExecutionValidationError("duplicate rule_id")
        grouped: dict[tuple[str, str], list[AShareTradingRule]] = {}
        for rule in self.rules:
            grouped.setdefault((rule.exchange, rule.board), []).append(rule)
        for scope, scoped_rules in grouped.items():
            ordered = tuple(sorted(scoped_rules, key=lambda item: item.effective_from))
            for left, right in zip(ordered, ordered[1:], strict=False):
                if left.effective_to >= right.effective_from:
                    raise ExecutionValidationError(
                        f"overlapping PIT rules: {left.rule_id}, {right.rule_id}"
                    )
            self._by_scope[scope] = ordered

    def resolve(
        self,
        exchange: str,
        board: str,
        trade_date: date,
    ) -> AShareTradingRule | None:
        matches = tuple(
            rule
            for rule in self._by_scope.get((exchange, board), ())
            if rule.applies_on(trade_date)
        )
        if len(matches) > 1:  # defensive: constructor already forbids this
            raise ExecutionValidationError("ambiguous PIT trading rule")
        return matches[0] if matches else None


@dataclass(frozen=True)
class InstrumentIdentity:
    instrument_id: str
    exchange: str
    board: str
    effective_from: date
    effective_to: date | None
    source_record_id: str

    def __post_init__(self) -> None:
        require_identifier(self.instrument_id, "instrument_id")
        require_identifier(self.exchange, "exchange")
        require_identifier(self.board, "board")
        require_identifier(self.source_record_id, "source_record_id")
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ExecutionValidationError("identity effective interval is reversed")

    def applies_on(self, value: date) -> bool:
        return self.effective_from <= value and (
            self.effective_to is None or value <= self.effective_to
        )


class PITIdentityBook:
    def __init__(self, records: tuple[InstrumentIdentity, ...]) -> None:
        self.records = tuple(records)
        self._by_instrument: dict[str, tuple[InstrumentIdentity, ...]] = {}
        grouped: dict[str, list[InstrumentIdentity]] = {}
        for record in self.records:
            grouped.setdefault(record.instrument_id, []).append(record)
        for instrument_id, instrument_records in grouped.items():
            ordered = tuple(
                sorted(instrument_records, key=lambda item: item.effective_from)
            )
            for left, right in zip(ordered, ordered[1:], strict=False):
                if (left.effective_to or date.max) >= right.effective_from:
                    raise ExecutionValidationError(
                        "overlapping PIT identity records for " + instrument_id
                    )
            self._by_instrument[instrument_id] = ordered

    def resolve(
        self,
        instrument_id: str,
        as_of: date,
    ) -> InstrumentIdentity | None:
        matches = tuple(
            record
            for record in self._by_instrument.get(instrument_id, ())
            if record.applies_on(as_of)
        )
        if len(matches) > 1:  # defensive: constructor already forbids this
            raise ExecutionValidationError("ambiguous PIT identity")
        return matches[0] if matches else None


@dataclass(frozen=True)
class TradingCalendar:
    sessions: tuple[date, ...]
    coverage_start: date
    coverage_end: date
    source_id: str
    source_sha256: str
    _session_set: frozenset[date] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        require_identifier(self.source_id, "calendar source_id")
        if self.coverage_end < self.coverage_start:
            raise ExecutionValidationError("calendar coverage interval is reversed")
        if tuple(sorted(set(self.sessions))) != self.sessions:
            raise ExecutionValidationError("calendar sessions must be unique and sorted")
        if any(
            session < self.coverage_start or session > self.coverage_end
            for session in self.sessions
        ):
            raise ExecutionValidationError("calendar session outside declared coverage")
        if len(self.source_sha256) != 64 or any(
            char not in "0123456789abcdef" for char in self.source_sha256
        ):
            raise ExecutionValidationError("calendar source_sha256 must be SHA-256")
        object.__setattr__(self, "_session_set", frozenset(self.sessions))

    def session_status(self, value: date) -> bool | None:
        if not self.coverage_start <= value <= self.coverage_end:
            return None
        return value in self._session_set

    def next_session(self, value: date) -> date | None:
        if self.session_status(value) is not True:
            return None
        return next((session for session in self.sessions if session > value), None)


_RETRIEVED = date(2026, 9, 6)
_LIMITATIONS = (
    "limit orders in continuous auction only",
    "price-limit bands, price cages, auctions, and order-book queues not modeled",
    "A-share common stock only; funds, bonds, B-shares, and block trades excluded",
)


def _source(
    authority: str,
    title: str,
    url: str,
    published_on: date,
    document_sha256: str,
) -> RuleSource:
    return RuleSource(
        authority=authority,
        title=title,
        url=url,
        published_on=published_on,
        retrieved_on=_RETRIEVED,
        document_sha256=document_sha256,
    )


SSE_2018 = _source(
    "Shanghai Stock Exchange",
    "上海证券交易所交易规则（2018年修订）",
    "https://www.sse.com.cn/lawandrules/sselawsrules/repeal/rules/c/10118961/"
    "files/ab243f7259464115b6522389422784b3.doc",
    date(2018, 8, 6),
    "0dd8168dfcd2b105a70459319b5f8dcdbc84eeb1216aad23f83214501ad5ce64",
)
SSE_2020 = _source(
    "Shanghai Stock Exchange",
    "上海证券交易所交易规则（2020年第二次修订）",
    "https://www.sse.com.cn/lawandrules/sselawsrules2025/repeal/rules/c/10785127/"
    "files/b39b8db2daa74eac916d8b3d6907af51.docx",
    date(2020, 3, 13),
    "bc0abb073a0463e3b9e0a3d8d0fa190ffee44286fdae8bf431f1f64fb8eccb2f",
)
SSE_STAR_2019 = _source(
    "Shanghai Stock Exchange",
    "上海证券交易所科创板股票交易特别规定",
    "https://www.sse.com.cn/lawandrules/sselawsrules/repeal/rules/c/10118601/"
    "files/f6fc4a1d4c1f469183a013c4dc36a535.pdf",
    date(2019, 3, 1),
    "e751bcc6470c49a4de98c5dce32d03e926927f0825d3f9c469274b802e748b6a",
)
SSE_STAR_MANUAL_2019 = _source(
    "Shanghai Stock Exchange",
    "科创板投资修炼手册",
    "https://edu.sse.com.cn/tib/manual/c/10608392/files/"
    "cd2f63893add490490f61a508b7c0ed2.pdf",
    date(2019, 7, 15),
    "ec5794dd026a4064aa637b4e410d13ae2e009debda41ada66aec6eaccaec507b",
)
SSE_2023 = _source(
    "Shanghai Stock Exchange",
    "上海证券交易所交易规则（2023年修订）",
    "https://www.sse.com.cn/lawandrules/sselawsrules2025/repeal/rules/c/10824490/"
    "files/dcbe58edb194451d93f19b1f7dd8fb4c.docx",
    date(2023, 2, 17),
    "7aa2319f6dcf597be1e86b3b69d7c2ad0e6acb2a5d0cc6be48a01af602fded40",
)
SZSE_2016 = _source(
    "Shenzhen Stock Exchange",
    "深圳证券交易所交易规则（2016年9月修订）",
    "https://docs.static.szse.cn/www/aboutus/trends/news/"
    "W020180328463044335575.pdf",
    date(2016, 9, 30),
    "0e593f31e0d519d61c65707afe07f046a441d3b412804d8ade8d6bd126b61db5",
)
SZSE_2020 = _source(
    "Shenzhen Stock Exchange",
    "深圳证券交易所交易规则（2020年修订）",
    "https://www.szse.cn/lawrules/rule/repeal/rules/"
    "P020231230545338442079.pdf",
    date(2020, 3, 13),
    "348218aab3164083e52057f7313d7d9d7e29f3701b464bc3cc030b600fc23215",
)
SZSE_2020_DEC = _source(
    "Shenzhen Stock Exchange",
    "深圳证券交易所交易规则（2020年12月修订）",
    "https://docs.static.szse.cn/www/disclosure/notice/general/"
    "W020201231711823503758.pdf",
    date(2020, 12, 31),
    "eead38ae76a326f8457fe81dfb5d7aba0ff57dd5783899d1d7d816969e6e5d93",
)
SZSE_2021 = _source(
    "Shenzhen Stock Exchange",
    "深圳证券交易所交易规则（2021年3月修订）",
    "https://www.szse.cn/lawrules/rule/repeal/rules/"
    "P020231230545143336292.pdf",
    date(2021, 3, 31),
    "3a7e3e378b169248bb9314accd749586a10af24028909e66ae0f1759d5151c3d",
)
SZSE_CHINEXT_2020 = _source(
    "Shenzhen Stock Exchange",
    "深圳证券交易所创业板交易特别规定",
    "https://docs.static.szse.cn/www/disclosure/notice/general/"
    "W020200612831351578076.pdf",
    date(2020, 6, 12),
    "c4ea293e2f1e86c5083fa7b606ceb5cf621885ff539e05bf089a7efd5fa7c20f",
)


def _rule(
    rule_id: str,
    version: str,
    exchange: str,
    board: str,
    start: date,
    end: date,
    sources: tuple[RuleSource, ...],
    *,
    minimum: int,
    step: int,
    maximum: int,
) -> AShareTradingRule:
    return AShareTradingRule(
        rule_id=rule_id,
        version=version,
        exchange=exchange,
        board=board,
        effective_from=start,
        effective_to=end,
        sources=sources,
        supported_order_types=(OrderType.LIMIT,),
        supported_sessions=(OrderSession.CONTINUOUS_AUCTION,),
        price_tick=Decimal("0.01"),
        buy_min_quantity=minimum,
        buy_quantity_step=step,
        sell_min_quantity=minimum,
        sell_quantity_step=step,
        max_limit_quantity=maximum,
        allow_full_odd_lot_exit=True,
        sellability_lag_sessions=1,
        limitations=_LIMITATIONS,
    )


def default_a_share_rule_book() -> PITRuleBook:
    """Rules whose exact source bytes were independently hashed.

    The missing SZSE interval from 2023-04-10 onward is intentional: the
    official historical attachment could not be byte-retrieved in this run,
    so the resolver returns ``None`` instead of borrowing the 2026 rule.
    """
    rules = (
        _rule(
            "sse-main-2018", "2018", "SSE", "MAIN",
            date(2018, 8, 6), date(2020, 3, 12), (SSE_2018,),
            minimum=100, step=100, maximum=1_000_000,
        ),
        _rule(
            "sse-main-2020", "2020.2", "SSE", "MAIN",
            date(2020, 3, 13), date(2023, 4, 9), (SSE_2020,),
            minimum=100, step=100, maximum=1_000_000,
        ),
        _rule(
            "sse-main-2023", "2023", "SSE", "MAIN",
            date(2023, 4, 10), date(2024, 12, 31), (SSE_2023,),
            minimum=100, step=100, maximum=1_000_000,
        ),
        _rule(
            "sse-star-2019-base-2018", "2019+2018", "SSE", "STAR",
            date(2019, 3, 1), date(2020, 3, 12),
            (SSE_STAR_2019, SSE_STAR_MANUAL_2019, SSE_2018),
            minimum=200, step=1, maximum=100_000,
        ),
        _rule(
            "sse-star-2019-base-2020", "2019+2020", "SSE", "STAR",
            date(2020, 3, 13), date(2023, 4, 9),
            (SSE_STAR_2019, SSE_STAR_MANUAL_2019, SSE_2020),
            minimum=200, step=1, maximum=100_000,
        ),
        _rule(
            "sse-star-2023", "2023", "SSE", "STAR",
            date(2023, 4, 10), date(2024, 12, 31), (SSE_2023,),
            minimum=200, step=1, maximum=100_000,
        ),
        _rule(
            "szse-main-2016", "2016.09", "SZSE", "MAIN",
            date(2016, 9, 30), date(2020, 3, 12), (SZSE_2016,),
            minimum=100, step=100, maximum=1_000_000,
        ),
        _rule(
            "szse-main-2020", "2020", "SZSE", "MAIN",
            date(2020, 3, 13), date(2020, 12, 30), (SZSE_2020,),
            minimum=100, step=100, maximum=1_000_000,
        ),
        _rule(
            "szse-main-2020-dec", "2020.12", "SZSE", "MAIN",
            date(2020, 12, 31), date(2021, 3, 30), (SZSE_2020_DEC,),
            minimum=100, step=100, maximum=1_000_000,
        ),
        _rule(
            "szse-main-2021", "2021.03", "SZSE", "MAIN",
            date(2021, 3, 31), date(2023, 4, 9), (SZSE_2021,),
            minimum=100, step=100, maximum=1_000_000,
        ),
        _rule(
            "szse-chinext-2016", "2016.09", "SZSE", "CHINEXT",
            date(2016, 9, 30), date(2020, 3, 12), (SZSE_2016,),
            minimum=100, step=100, maximum=1_000_000,
        ),
        _rule(
            "szse-chinext-2020", "2020", "SZSE", "CHINEXT",
            date(2020, 3, 13), date(2020, 8, 23), (SZSE_2020,),
            minimum=100, step=100, maximum=1_000_000,
        ),
        _rule(
            "szse-chinext-special-2020", "2020-special", "SZSE", "CHINEXT",
            date(2020, 8, 24), date(2020, 12, 30),
            (SZSE_CHINEXT_2020, SZSE_2020),
            minimum=100, step=100, maximum=300_000,
        ),
        _rule(
            "szse-chinext-special-2020-dec", "2020-special+2020.12",
            "SZSE", "CHINEXT", date(2020, 12, 31), date(2021, 3, 30),
            (SZSE_CHINEXT_2020, SZSE_2020_DEC),
            minimum=100, step=100, maximum=300_000,
        ),
        _rule(
            "szse-chinext-special-2021", "2020-special+2021.03",
            "SZSE", "CHINEXT", date(2021, 3, 31), date(2023, 4, 9),
            (SZSE_CHINEXT_2020, SZSE_2021),
            minimum=100, step=100, maximum=300_000,
        ),
    )
    return PITRuleBook(rules)
