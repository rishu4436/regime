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

    @property
    def has_replay(self) -> bool:
        """Duck-typed: Bot 4's SimulatedBinanceClient pins candles/prices/quotes to a bar via set_replay_index(i)."""
        return callable(getattr(self.exchange, "set_replay_index", None))

    def _total(self, symbol: str) -> int:
        n_bars = getattr(self.exchange, "n_bars", None)
        return int(n_bars(symbol)) if callable(n_bars) else self.total  # replay indices refer to the client's series

    def series(self, symbol: str, timeframe: Timeframe) -> list[Candle]:
        k = (symbol, timeframe)
        if k not in self._series:
            if self.has_replay:  # fetch the FULL series, then restore the client's current bar
                cur = getattr(self.exchange, "replay_index", None)
                self.exchange.set_replay_index(None)
                try:
                    self._series[k] = self.exchange.get_candles(symbol, timeframe, limit=self._total(symbol))
                finally:
                    self.exchange.set_replay_index(cur)
            else:
                self._series[k] = self.exchange.get_candles(symbol, timeframe, limit=self._total(symbol))
            self._cursor[k] = min(self.warmup, len(self._series[k])) - 1
        return self._series[k]

    def start_time(self, symbol: str, timeframe: Timeframe):
        """Timestamp of the bar the replay of `symbol` currently sits on (warm-up end before the first tick)."""
        s = self.series(symbol, timeframe)
        return s[max(self._cursor[(symbol, timeframe)], 0)].timestamp if s else None

    def next_window(self, symbol: str, timeframe: Timeframe) -> Optional[list[Candle]]:
        s = self.series(symbol, timeframe)
        k = (symbol, timeframe)
        if self._cursor[k] + 1 >= len(s):
            return None  # replay finished
        self._cursor[k] += 1
        i = self._cursor[k]
        if self.has_replay:
            self.exchange.set_replay_index(i)  # candles end at bar i; price / quotes / fills use bar i close
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
