"""Market feeds. SIM replays the (seeded, deterministic) mock series bar by bar so a whole Manual/Auto session is
reproducible; PAPER/LIVE poll the latest candles and only act once per new bar."""
from __future__ import annotations

import threading
from typing import Optional

from backend.contracts import SIM_WARMUP_BARS, Candle, ExchangeClient, Timeframe


class ReplayFeed:
    """SIM bar-by-bar replay over a cached, full, seeded series per (symbol, timeframe).

    The full series are built ONCE, eagerly, when the feed is created - i.e. before the loop ever pins the client
    with set_replay_index(i) - and cached under a lock. The feed never toggles set_replay_index to fetch history
    (that raced with the live loop); the only call is the per-tick pin in next_window(). Backtests and
    `/api/candles?history=true` read the same cached bars, so they do not move while the loop advances.
    """

    def __init__(self, exchange: ExchangeClient, total_bars: int = 500, window: int = 200, warmup: int = SIM_WARMUP_BARS) -> None:
        self.exchange, self.total, self.window, self.warmup = exchange, total_bars, window, warmup
        self._series: dict[tuple[str, Timeframe], list[Candle]] = {}
        self._cursor: dict[tuple[str, Timeframe], int] = {}
        self._active: dict[str, Timeframe] = {}  # symbol -> timeframe of the last next_window
        self._clock: dict[str, object] = {}  # symbol -> timestamp of the bar the replay last pinned (any timeframe)
        self._lock = threading.RLock()
        if self.has_replay:
            self._prebuild()

    def _prebuild(self) -> None:
        """Every token x timeframe, while the client is still unpinned (cheap: seeded mock data, no network)."""
        try:
            symbols = [t.symbol for t in self.exchange.search_tokens("")]
        except Exception:  # pragma: no cover - a client without a token list falls back to lazy building
            symbols = []
        for sym in symbols:
            for tf in Timeframe:
                self.series(sym, tf)
        self._cursor.clear()  # cursors are set on first use (so a later timeframe resumes at the replay clock)

    def reset(self, symbol: str, timeframe: Timeframe) -> None:
        with self._lock:
            self._cursor.pop((symbol, timeframe), None)  # the cached history stays

    @property
    def has_replay(self) -> bool:
        """Duck-typed: Bot 4's SimulatedBinanceClient pins candles/prices/quotes to a bar via set_replay_index(i)."""
        return callable(getattr(self.exchange, "set_replay_index", None))

    def _total(self, symbol: str, timeframe: Timeframe = Timeframe.H1) -> int:
        n_bars = getattr(self.exchange, "n_bars", None)
        if not callable(n_bars):
            return self.total
        try:  # Bot 4 aea22d1: n_bars(symbol, timeframe) - finer timeframes have more bars over the same span
            return int(n_bars(symbol, timeframe))
        except TypeError:
            return int(n_bars(symbol))  # replay indices refer to the client's series

    def _fetch(self, symbol: str, timeframe: Timeframe) -> list[Candle]:
        full = getattr(self.exchange, "get_full_candles", None)  # ignores the pin (safe while the loop is pinned)
        if callable(full):
            try:
                return list(full(symbol, timeframe))
            except TypeError:
                pass
        return list(self.exchange.get_candles(symbol, timeframe, limit=self._total(symbol, timeframe)))

    def _resume_index(self, symbol: str, timeframe: Timeframe) -> Optional[int]:
        """Timeframe switch: the bar of `timeframe` at the replay clock of `symbol` (Bot 4 index_at), else None."""
        t = self._clock.get(symbol)
        fn = getattr(self.exchange, "index_at", None)
        if t is None or not callable(fn):
            return None
        try:
            return int(fn(symbol, timeframe, t))
        except Exception:
            return None

    def _pin(self, i: int, timeframe: Timeframe) -> None:
        try:
            self.exchange.set_replay_index(i, timeframe)  # Bot 4 aea22d1: the pin is a time, shared by every timeframe
        except TypeError:
            self.exchange.set_replay_index(i)  # older client: H1 index only

    def series(self, symbol: str, timeframe: Timeframe) -> list[Candle]:
        """Full cached history (oldest first). Built on first use; with a replay client that is feed construction."""
        k = (symbol, timeframe)
        with self._lock:
            if k not in self._series:
                # Only reached lazily for clients without set_replay_index (nothing to race with) or for a symbol that
                # was not in the token list at start-up (then the client's current view is the best we can get).
                self._series[k] = self._fetch(symbol, timeframe)
            if k not in self._cursor:
                j = self._resume_index(symbol, timeframe)
                self._cursor[k] = j if j is not None and 0 <= j < len(self._series[k]) else min(self.warmup, len(self._series[k])) - 1
            return self._series[k]

    def history(self, symbol: str, timeframe: Timeframe, limit: Optional[int] = None) -> list[Candle]:
        s = self.series(symbol, timeframe)
        return list(s if not limit else s[-limit:])

    def start_time(self, symbol: str, timeframe: Timeframe):
        """Timestamp of the bar the replay of `symbol` currently sits on (warm-up end before the first tick)."""
        with self._lock:
            s = self.series(symbol, timeframe)
            return s[max(self._cursor[(symbol, timeframe)], 0)].timestamp if s else None

    def next_window(self, symbol: str, timeframe: Timeframe) -> Optional[list[Candle]]:
        with self._lock:
            s = self.series(symbol, timeframe)
            k = (symbol, timeframe)
            if self._active.get(symbol, timeframe) != timeframe:  # timeframe switch: resume at the same replay time
                j = self._resume_index(symbol, timeframe)
                if j is not None and 0 <= j < len(s):
                    self._cursor[k] = j
            self._active[symbol] = timeframe
            if self._cursor[k] + 1 >= len(s):
                return None  # replay finished
            self._cursor[k] += 1
            i = self._cursor[k]
            if self.has_replay:
                self._pin(i, timeframe)  # candles end at bar i; price / quotes / fills use bar i close
            self._clock[symbol] = s[i].timestamp
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
