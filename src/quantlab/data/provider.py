"""Abstract market data provider."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

from quantlab.data.models import DailyBar, Security, TradingCalendar


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
