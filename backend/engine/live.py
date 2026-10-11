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
from typing import Any, Mapping, Optional, Sequence

from backend.contracts import Candle, MarketHoursFilter

from .data import FAMILY_ONDO, FAMILY_XSTOCK, STOCK_TOKEN_TYPES, as_utc, infer_token_type, staleness, token_family
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
    # Token families (data.token_family): xStock has ~zero liquidity on BSC -> no entries by default.
    blocked_families: frozenset = frozenset({FAMILY_XSTOCK})
    # Families whose candles carry no on-chain bar volume (Ondo off-hours: volume 0, price ~= reference): volume
    # conditions / the min-volume filter are NOT APPLICABLE and the no-volume / thin-liquidity gates are skipped.
    # Stale-data refusal still applies.
    volume_exempt_families: frozenset = frozenset({FAMILY_ONDO})
    # Minimum order per family (Ondo rejects < 20 USD with 40375). A smaller BUY is bumped up to the floor if the
    # floor <= max_order_usdt (and <= the equity basis), else STAY_OUT 'live_below_min_order'.
    min_order_usdt: Mapping[str, float] = field(default_factory=lambda: {FAMILY_ONDO: 20.0})
    bump_to_min_order: bool = True
    max_order_usdt: float = 25.0  # never propose more than this per BUY (Bot 3's live per-order cap)
    equity_hint_usdt: Optional[float] = None  # if set, size from min(equity, hint) (e.g. the real wallet balance)
    # Market context (DefaultStrategyEngine.set_market_context): quote price impact gates.
    impact_halve_pct: float = 1.0  # impact > 1% -> size x0.5
    impact_max_pct: float = 2.0  # impact > 2% -> STAY_OUT 'live_price_impact'

    def __post_init__(self) -> None:
        if not (0 < self.size_factor <= 1) or not (0 < self.max_size_pct <= 100):
            raise ValueError("size_factor must be in (0, 1] and max_size_pct in (0, 100]")
        if self.max_stale_bars <= 0 or self.max_future_bars < 0:
            raise ValueError("max_stale_bars must be > 0 and max_future_bars >= 0")
        if self.liquidity_lookback < 1 or not (0 <= self.min_active_fraction <= self.thin_active_fraction <= 1):
            raise ValueError("liquidity_lookback >= 1 and 0 <= min_active_fraction <= thin_active_fraction <= 1")
        if not (0 < self.thin_size_factor <= 1) or self.thin_relative_volume < 0:
            raise ValueError("thin_size_factor must be in (0, 1] and thin_relative_volume >= 0")
        if self.max_order_usdt <= 0 or any(v < 0 for v in self.min_order_usdt.values()):
            raise ValueError("max_order_usdt must be > 0 and min_order_usdt values >= 0")
        if self.equity_hint_usdt is not None and self.equity_hint_usdt <= 0:
            raise ValueError("equity_hint_usdt must be > 0")
        if not (0 < self.impact_halve_pct <= self.impact_max_pct):
            raise ValueError("0 < impact_halve_pct <= impact_max_pct")


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


def volume_applicable(symbol: str, cfg: LiveSafetyConfig) -> bool:
    return token_family(symbol) not in cfg.volume_exempt_families


def entry_blockers(
    symbol: str, candles: Sequence[Candle], bar_seconds: int, now: datetime, cfg: LiveSafetyConfig
) -> list[tuple[str, str]]:
    """(name, human reason) for every live-safety rule that blocks a NEW entry. Empty list = allowed."""
    out: list[tuple[str, str]] = []
    fam = token_family(symbol)
    if fam in cfg.blocked_families:
        out.append(("live_illiquid_family", f"{symbol} is a {fam} token: no usable on-chain liquidity on BSC"))
    if not candles:
        return out + [("live_no_data", "no valid candles")]
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
    if fam in cfg.volume_exempt_families:  # e.g. Ondo: zero bar volume is normal, not a liquidity signal
        return out
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


# ---------------------------------------------------------------- market context (optional, from the loop)
PAUSED_STATUSES = frozenset({"paused", "pause", "closed"})
PAUSED_REASONS = frozenset({"ASSET_PAUSED", "MARKET_PAUSED", "MAINTENANCE", "MARKET_MAINTENANCE", "UNSUPPORTED"})


def _num(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def normalize_market_context(ctx: Optional[Mapping[str, Any]]) -> Optional[dict]:
    """Accepts the engine shape ``{status, price_impact_pct, reference_price, equity}`` and the raw Binance
    ``statusInfo`` shape (``marketStatus``/``openState``/``reasonCode``). Unknown keys are ignored."""
    if ctx is None:
        return None
    status = ctx.get("status", ctx.get("marketStatus"))
    return {
        "status": str(status).lower() if status is not None else None,
        "open_state": ctx.get("openState", ctx.get("open_state")),
        "reason_code": (str(ctx.get("reasonCode", ctx.get("reason_code")) or "").upper() or None),
        "price_impact_pct": _num(ctx.get("price_impact_pct", ctx.get("priceImpactPercent"))),
        "reference_price": _num(ctx.get("reference_price", ctx.get("referencePrice"))),
        "equity": _num(ctx.get("equity")),
    }


def market_blockers(symbol: str, ctx: Optional[dict], cfg: LiveSafetyConfig) -> list[tuple[str, str]]:
    if not ctx:
        return []
    out = []
    st, rc = ctx.get("status"), ctx.get("reason_code")
    if st in PAUSED_STATUSES or ctx.get("open_state") is False or rc in PAUSED_REASONS:
        out.append(
            ("live_token_paused", f"{symbol} not tradable now (status {st}, openState {ctx.get('open_state')}, {rc})")
        )
    imp = ctx.get("price_impact_pct")
    if imp is not None and imp > cfg.impact_max_pct:
        out.append(("live_price_impact", f"quote price impact {imp:.2f}% > {cfg.impact_max_pct:g}%"))
    return out


@dataclass
class SizeDecision:
    pct: float
    notional: float
    note: str
    blocker: Optional[tuple[str, str]] = None


def live_size(
    symbol: str,
    proposed_pct: float,
    equity: float,
    candles: Sequence[Candle],
    cfg: LiveSafetyConfig,
    ctx: Optional[dict] = None,
) -> SizeDecision:
    """Live-safe BUY size: x size_factor, cap max_size_pct, x thin-liquidity factor (volume families only),
    x0.5 on price impact > impact_halve_pct; then the per-family min-order floor and the max_order_usdt cap."""
    fam = token_family(symbol)
    basis = equity
    hint = ctx.get("equity") if ctx else None
    for h in (hint, cfg.equity_hint_usdt):
        if h is not None and h > 0:
            basis = min(basis, h)
    steps = [f"{proposed_pct:g}% x{cfg.size_factor:g} capped at {cfg.max_size_pct:g}%"]
    pct = min(proposed_pct * cfg.size_factor, cfg.max_size_pct)
    if fam not in cfg.volume_exempt_families:
        factor, thin = thin_liquidity_factor(candles, cfg)
        if thin:
            pct *= factor
            steps.append(thin)
    halved = False
    imp = ctx.get("price_impact_pct") if ctx else None
    if imp is not None and imp > cfg.impact_halve_pct:
        pct *= 0.5
        halved = True
        steps.append(f"price impact {imp:.2f}% > {cfg.impact_halve_pct:g}% x0.5")
    notional = basis * pct / 100
    if basis != equity:
        steps.append(f"equity basis {basis:g}")
    floor = cfg.min_order_usdt.get(fam, 0.0)
    if floor > 0 and notional < floor:
        can_bump = cfg.bump_to_min_order and not halved and floor <= cfg.max_order_usdt and floor <= basis
        if not can_bump:
            why = (
                "price impact halved the size"
                if halved
                else f"floor > max order {cfg.max_order_usdt:g} USDT"
                if floor > cfg.max_order_usdt
                else f"floor > equity {basis:g}"
                if floor > basis
                else "bumping disabled"
            )
            msg = f"{notional:.2f} USDT is below the {fam} minimum order {floor:g} USDT ({why})"
            return SizeDecision(0.0, 0.0, "", ("live_below_min_order", msg))
        steps.append(f"bumped {notional:.2f} -> {fam} min order {floor:g} USDT")
        notional = floor
    if notional > cfg.max_order_usdt:
        steps.append(f"capped at {cfg.max_order_usdt:g} USDT")
        notional = cfg.max_order_usdt
    pct = notional / equity * 100 if equity > 0 else 0.0  # Signal convention: % of the engine's equity/capital
    note = f" [live-safe size {', '.join(steps)} -> {pct:.4g}% = {notional:.2f} USDT]"
    return SizeDecision(pct, notional, note)
