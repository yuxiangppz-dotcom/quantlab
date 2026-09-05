"""Abstract market data provider."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

from quantlab.data.models import (
    AdjFactor,
    DailyBar,
    DailyBasic,
    IndexDailyBar,
    NameChangeRecord,
    RawLifecycleAnnouncement,
    Security,
    StockSTStatus,
    SuspensionRecord,
    TradingCalendar,
)


class DataProvider(ABC):
    """Interface for market data sources."""

    @abstractmethod
    def get_securities(self) -> list[Security]:
        """Return the A-share security master list."""

    @abstractmethod
    def get_trading_calendar(self, start_date: date, end_date: date) -> list[TradingCalendar]:
        """Return trading calendar entries between ``start_date`` and ``end_date``."""

    @abstractmethod
    def get_daily_bars(
        self,
        instrument_ids: list[str],
        start_date: date,
        end_date: date,
    ) -> list[DailyBar]:
        """Return daily bars for the given instruments and date range."""

    @abstractmethod
    def get_daily_bars_by_date(self, trade_date: date) -> list[DailyBar]:
        """Return the full-market daily bars for a single trading date."""

    @abstractmethod
    def get_adj_factors_by_date(self, trade_date: date) -> list[AdjFactor]:
        """Return the full-market adjustment factors for a single trading date."""

    @abstractmethod
    def get_daily_basic_by_date(self, trade_date: date) -> list[DailyBasic]:
        """Return the full-market daily basic metrics for a single trading date."""

    @abstractmethod
    def get_lifecycle_announcements_by_date(
        self, announcement_date: date
    ) -> list[RawLifecycleAnnouncement]:
        """Return raw announcement-index records published on one calendar day."""

    @abstractmethod
    def get_stock_st_by_date(self, trade_date: date) -> list[StockSTStatus]:
        """Return the full ST-status snapshot for one trading session."""

    @abstractmethod
    def get_suspensions_by_date(self, trade_date: date) -> list[SuspensionRecord]:
        """Return raw S/R suspension facts for one trading session."""

    @abstractmethod
    def get_name_changes(
        self, instrument_id: str, start_date: date, end_date: date
    ) -> list[NameChangeRecord]:
        """Return source-provided name history for one explicitly scoped instrument."""

    @abstractmethod
    def get_index_daily(
        self, instrument_id: str, start_date: date, end_date: date
    ) -> list[IndexDailyBar]:
        """Return index daily bars for one index instrument and date range."""
