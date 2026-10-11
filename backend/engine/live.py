"""Live / mainnet safety layer for the engine (``DefaultStrategyEngine(live_safe=True)``).

It only ever makes the engine MORE conservative; the risk layer (Bot 3) still enforces the hard USDT caps
(25 USDT per order, 25 USDT daily loss, 50 USDT exposure). The agent runs fully autonomously 24/7 (no per-trade
approval), so this layer is the engine's last line of defence:

* BUY blocked when the data is stale (default > 2 bars old vs ``now``) or from the future, the last bar has no
  volume, liquidity over the last ``liquidity_lookback`` bars is too thin, or (manual) an enabled indicator is
  still warming up.
* BUY size: proposed x ``size_factor``, capped at ``max_size_pct``, and x ``thin_size_factor`` again when liquidity
  is thin-but-tradeable (overnight / weekend on-chain hours).
* Market-hours gating is OFF by default (tokenized stocks trade on-chain around the clock); enable it with
  ``market_hours=MarketHoursFilter(enabled=True)`` to only enter during the US session (stock tokens only).
* SELL signals are never blocked here (reducing exposure is always allowed).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Sequence

from backend.contracts import Candle, MarketHoursFilter

from .data import STOCK_TOKEN_TYPES, as_utc, infer_token_type, staleness
from .manual import in_market_hours


@dataclass(frozen=True)
class LiveSafetyConfig:
    size_factor: float = 0.25  # proposed size x0.25 ...
    max_size_pct: float = 2.0  # ... and never above 2% of capital/equity (e.g. 20 USDT on 1000)
    max_stale_bars: float = 2.0  # last candle OPEN older than 2 bars vs now -> stale
    max_future_bars: float = 1.0  # last candle more than 1 bar in the future -> clock skew / bad data
    # OFF by default: the agent trades 24/7 and tokenized stocks may trade on-chain around the clock.
    market_hours: MarketHoursFilter = field(default_factory=lambda: MarketHoursFilter(enabled=False))
    require_volume: bool = True  # last bar must have volume > 0
    require_all_conditions_ready: bool = True  # manual: every enabled entry indicator must be out of warm-up
    # Thin liquidity (24/7 data: overnight / weekend on-chain hours), measured over the last N bars:
    liquidity_lookback: int = 20
    min_active_fraction: float = 0.25  # < 25% of bars traded -> no entry
    thin_active_fraction: float = 0.6  # < 60% of bars traded -> size reduced
    thin_relative_volume: float = 0.25  # last bar volume < 25% of the window's mean traded volume -> size reduced
    thin_size_factor: float = 0.5

    def __post_init__(self) -> None:
        if not (0 < self.size_factor <= 1) or not (0 < self.max_size_pct <= 100):
            raise ValueError("size_factor must be in (0, 1] and max_size_pct in (0, 100]")
        if self.max_stale_bars <= 0 or self.max_future_bars < 0:
            raise ValueError("max_stale_bars must be > 0 and max_future_bars >= 0")
        if self.liquidity_lookback < 1 or not (0 <= self.min_active_fraction <= self.thin_active_fraction <= 1):
            raise ValueError("liquidity_lookback >= 1 and 0 <= min_active_fraction <= thin_active_fraction <= 1")
        if not (0 < self.thin_size_factor <= 1) or self.thin_relative_volume < 0:
            raise ValueError("thin_size_factor must be in (0, 1] and thin_relative_volume >= 0")


@dataclass(frozen=True)
class Liquidity:
    active_fraction: float  # share of the last N bars with volume > 0
    relative_volume: float  # last bar volume / mean volume of the traded bars in the window (0 if none)
    bars: int

    def describe(self) -> str:
        pct = self.active_fraction * 100
        return f"{pct:.0f}% of last {self.bars} bars traded, last bar {self.relative_volume:.2f}x avg"


def liquidity(candles: Sequence[Candle], lookback: int) -> Liquidity:
    window = list(candles[-lookback:])
    if not window:
        return Liquidity(0.0, 0.0, 0)
    traded = [c.volume for c in window if c.volume > 0]
    mean = sum(traded) / len(traded) if traded else 0.0
    rel = window[-1].volume / mean if mean > 0 else 0.0
    return Liquidity(len(traded) / len(window), rel, len(window))


def thin_liquidity_factor(candles: Sequence[Candle], cfg: LiveSafetyConfig) -> tuple[float, Optional[str]]:
    """(size multiplier, note). 1.0 when liquidity is normal."""
    liq = liquidity(candles, cfg.liquidity_lookback)
    if liq.active_fraction < cfg.thin_active_fraction or liq.relative_volume < cfg.thin_relative_volume:
        return cfg.thin_size_factor, f"thin liquidity ({liq.describe()}) x{cfg.thin_size_factor:g}"
    return 1.0, None


def cap_size_pct(pct: float, cfg: LiveSafetyConfig, liquidity_factor: float = 1.0) -> float:
    return min(pct * cfg.size_factor, cfg.max_size_pct) * liquidity_factor


def entry_blockers(
    symbol: str, candles: Sequence[Candle], bar_seconds: int, now: datetime, cfg: LiveSafetyConfig
) -> list[tuple[str, str]]:
    """(name, human reason) for every live-safety rule that blocks a NEW entry. Empty list = allowed."""
    out: list[tuple[str, str]] = []
    if not candles:
        return [("live_no_data", "no valid candles")]
    last = candles[-1]
    now = as_utc(now)
    age = staleness(last.timestamp, now, bar_seconds)
    if age > cfg.max_stale_bars:
        out.append(
            (
                "live_stale_data",
                f"last candle {last.timestamp.isoformat()} is {age:.1f} bars old "
                f"(> {cfg.max_stale_bars:g}) at {now.isoformat()}",
            )
        )
    elif age < -cfg.max_future_bars:
        out.append(
            ("live_future_data", f"last candle {last.timestamp.isoformat()} is in the future vs {now.isoformat()}")
        )
    if infer_token_type(symbol) in STOCK_TOKEN_TYPES and cfg.market_hours.enabled:
        mh = cfg.market_hours
        window = f"{mh.start_hour_utc:02d}:00-{mh.end_hour_utc:02d}:00 UTC{' weekdays' if mh.weekdays_only else ''}"
        if not in_market_hours(last.timestamp, mh):
            out.append(
                ("live_market_hours", f"last bar {last.timestamp.isoformat()} is outside the stock session {window}")
            )
        if not in_market_hours(now, mh):
            out.append(("live_market_hours_now", f"now ({now.isoformat()}) is outside the stock session {window}"))
    if cfg.require_volume and last.volume <= 0:
        out.append(("live_no_volume", "last bar has no on-chain volume (illiquid / closed)"))
    liq = liquidity(candles, cfg.liquidity_lookback)
    if liq.active_fraction < cfg.min_active_fraction:
        out.append(
            (
                "live_thin_liquidity",
                f"too little on-chain trading ({liq.describe()}; need >= "
                f"{cfg.min_active_fraction * 100:.0f}% of bars traded)",
            )
        )
    return out
