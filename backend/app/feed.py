"""Market feeds. SIM replays the (seeded, deterministic) mock series bar by bar so a whole Manual/Auto session is
reproducible; PAPER/LIVE poll the latest candles and only act once per new bar."""
from __future__ import annotations

from typing import Optional

from backend.contracts import SIM_WARMUP_BARS, Candle, ExchangeClient, Timeframe


class ReplayFeed:
    def __init__(self, exchange: ExchangeClient, total_bars: int = 500, window: int = 200, warmup: int = SIM_WARMUP_BARS) -> None:
        self.exchange, self.total, self.window, self.warmup = exchange, total_bars, window, warmup
        self._series: dict[tuple[str, Timeframe], list[Candle]] = {}
        self._cursor: dict[tuple[str, Timeframe], int] = {}

    def reset(self, symbol: str, timeframe: Timeframe) -> None:
        self._series.pop((symbol, timeframe), None)
        self._cursor.pop((symbol, timeframe), None)

    def series(self, symbol: str, timeframe: Timeframe) -> list[Candle]:
        k = (symbol, timeframe)
        if k not in self._series:
            self._series[k] = self.exchange.get_candles(symbol, timeframe, limit=self.total)
            self._cursor[k] = min(self.warmup, len(self._series[k])) - 1
        return self._series[k]

    def next_window(self, symbol: str, timeframe: Timeframe) -> Optional[list[Candle]]:
        s = self.series(symbol, timeframe)
        k = (symbol, timeframe)
        if self._cursor[k] + 1 >= len(s):
            return None  # replay finished
        self._cursor[k] += 1
        i = self._cursor[k]
        return s[max(0, i + 1 - self.window):i + 1]

    def position(self, symbol: str, timeframe: Timeframe) -> tuple[int, int]:
        self.series(symbol, timeframe)
        return self._cursor[(symbol, timeframe)], len(self._series[(symbol, timeframe)])


class PollingFeed:
    def __init__(self, exchange: ExchangeClient, window: int = 200) -> None:
        self.exchange, self.window = exchange, window
        self._last: dict[tuple[str, Timeframe], object] = {}

    def reset(self, symbol: str, timeframe: Timeframe) -> None:
        self._last.pop((symbol, timeframe), None)

    def next_window(self, symbol: str, timeframe: Timeframe) -> Optional[list[Candle]]:
        c = self.exchange.get_candles(symbol, timeframe, limit=self.window)
        if not c or self._last.get((symbol, timeframe)) == c[-1].timestamp:
            return None
        self._last[(symbol, timeframe)] = c[-1].timestamp
        return c
