"""DefaultStrategyEngine - implements backend.contracts.StrategyEngine.

Pure: no I/O, no execution, no risk decisions.
"""

from __future__ import annotations

from dataclasses import replace

from datetime import datetime
from typing import Any, Callable, Mapping, Optional, Sequence

from backend.contracts import (
    SIM_WARMUP_BARS,
    Action,
    AgentMode,
    AutoConfig,
    BacktestResult,
    Candle,
    ExitReason,
    ManualStrategy,
    Portfolio,
    Position,
    PresetName,
    Regime,
    RegimeAnalysis,
    Signal,
    StrategySelection,
    TraderStyle,
    utc_now,
)

from . import exits, presets
from .backtest import BacktestRun, run_backtest
from .data import TIMEFRAME_SECONDS, DataReport, sanitize_candles
from .live import (
    LiveSafetyConfig,
    entry_blockers,
    live_size,
    market_blockers,
    normalize_market_context,
    volume_applicable,
)
from .manual import ManualIndicators, check_filters, evaluate_entry, indicator_exit
from .regime import detect_regime as _detect_regime


def manual_strategy_label(strategy: ManualStrategy) -> str:
    return f"MANUAL:{strategy.name}"


class DefaultStrategyEngine:
    """Stateless strategy & regime engine.

    ``fee_pct`` / ``slippage_bps`` are the defaults used by ``backtest_manual`` (0 = simulator parity);
    ``warmup_bars`` is the first bar index the backtest trades on (simulator: 60); live evaluation also needs at
    least ``warmup_bars + 1`` valid candles before it will propose an entry.
    ``live_safe=True`` (PAPER / LIVE) adds the live-safety layer of ``live.py``; ``clock`` gives 'now' (injectable).
    """

    def __init__(
        self,
        fee_pct: float = 0.0,
        slippage_bps: float = 0.0,
        warmup_bars: int = SIM_WARMUP_BARS,
        manual_price_exits: bool = False,
        live_safe: bool = False,
        live_config: Optional[LiveSafetyConfig] = None,
        clock: Callable[[], datetime] = utc_now,
        market_context: Optional[Mapping[str, Mapping[str, Any]]] = None,
    ) -> None:
        if fee_pct < 0 or slippage_bps < 0 or warmup_bars < 0:
            raise ValueError("fee_pct, slippage_bps and warmup_bars must be >= 0")
        self.fee_pct = fee_pct
        self.slippage_bps = slippage_bps
        self.warmup_bars = warmup_bars
        # Live SL/TP/trailing/max-loss are enforced by RiskManager.check_exits, which the loop runs BEFORE the engine
        # and after which the app raises Position.highest_price with the current bar's high. Re-checking price exits
        # here would duplicate them and use a stale (already raised) high-water mark, so they are off by default.
        self.manual_price_exits = manual_price_exits
        # Live / mainnet: extra BUY gates + smaller sizes (see live.py). Off by default (SIM/backtests unchanged).
        self.live_safe = live_safe
        self.live_config = live_config or LiveSafetyConfig()
        self.clock = clock
        # Optional per-symbol market context from the loop (no contract change): see set_market_context.
        self._market: dict[str, dict] = {}
        for sym, ctx in (market_context or {}).items():
            self.set_market_context(sym, ctx)

    def set_live_limits(self, max_order_usdt: Optional[float] = None, max_size_pct: Optional[float] = None) -> None:
        """Apply user-set trading limits at runtime (no restart). ``max_order_usdt``: the per-order USDT cap, kept
        equal to risk's live per-order cap (the 25 USDT default); ``max_size_pct``: the Auto-mode size cap in % of
        equity. ``None`` leaves a value unchanged. Risk still enforces every hard cap; this only stops the engine
        from proposing less than the user allowed."""
        upd: dict[str, float] = {}
        if max_order_usdt is not None:
            upd["max_order_usdt"] = float(max_order_usdt)
        if max_size_pct is not None:
            if not 0 < float(max_size_pct) <= 100:
                raise ValueError("max_size_pct must be in (0, 100]")
            upd["max_size_pct"] = float(max_size_pct)
        if upd:
            self.live_config = replace(self.live_config, **upd)  # __post_init__ re-validates

    def set_market_context(self, symbol: str, ctx: Optional[Mapping[str, Any]]) -> None:
        """Latest market info for ``symbol`` (``None`` clears it). Keys: ``status`` ('trading'|'open'|'off-hours'|
        'closed'|'no liquidity'|'paused'; Binance ``statusInfo`` marketStatus/openState/reasonCode also accepted),
        ``reference_price_only``, ``price_impact_pct`` or ``priceImpactPercent`` (percent units, from a quote),
        ``reference_price``, ``equity`` (USDT hint), ``min_order_usdt`` / ``minOrderUsdt`` (token minimum order,
        overrides the per-family default). Applies to BUYs only: off-hours / closed / no liquidity /
        paused / reference_price_only -> STAY_OUT 'live_token_paused'; |impact| > max_price_impact_pct (1%) ->
        STAY_OUT 'live_price_impact' (impact None / unparseable = unknown: no block). SELLs are never blocked.
        """
        norm = normalize_market_context(ctx)
        if norm is None:
            self._market.pop(symbol, None)
        else:
            self._market[symbol] = norm

    def market_context(self, symbol: str) -> Optional[dict]:
        return self._market.get(symbol)

    # ------------------------------------------------------------------ live helpers
    def _live_block(
        self, symbol: str, cs: Sequence[Candle], bar_seconds: int, base: dict, met: Sequence[str] = ()
    ) -> Optional[Signal]:
        # Token status gates whenever a context is set; the price-impact gate is part of live_safe.
        blocks = market_blockers(symbol, self._market.get(symbol), self.live_config, check_impact=self.live_safe)
        if self.live_safe:
            blocks = entry_blockers(symbol, cs, bar_seconds, self.clock(), self.live_config) + blocks
        if not blocks:
            return None
        return Signal(
            action=Action.STAY_OUT,
            conditions_met=list(met),
            conditions_failed=[n for n, _ in blocks],
            reason="Live safety blocked entry: " + "; ".join(r for _, r in blocks),
            **base,
        )

    def _size(
        self,
        symbol: str,
        pct: float,
        equity: float,
        cs: Sequence[Candle],
        base: dict,
        met: Sequence[str] = (),
        user_sized: bool = False,
    ) -> tuple[float, float, str, Optional[Signal]]:
        """(size %, notional, reason note, blocking STAY_OUT or None) for a BUY."""
        ctx = self._market.get(symbol)
        if not self.live_safe:
            return pct, equity * pct / 100, "", None
        d = live_size(symbol, pct, equity, cs, self.live_config, ctx, user_sized=user_sized)
        if d.blocker is not None:
            name, why = d.blocker
            blocked = Signal(
                action=Action.STAY_OUT,
                conditions_met=list(met),
                conditions_failed=[name],
                reason=f"Live safety blocked entry: {why}",
                **base,
            )
            return 0.0, 0.0, "", blocked
        return d.pct, d.notional, d.note, None

    @staticmethod
    def _data_note(rep: DataReport) -> str:
        return f" [data: {'; '.join(rep.issues)}]" if rep.issues else ""

    # ------------------------------------------------------------------ regime / selection
    def detect_regime(self, candles: Sequence[Candle]) -> RegimeAnalysis:
        return _detect_regime(candles)

    def select_strategy(
        self, regime: Regime, style: TraderStyle, enabled_presets: Sequence[PresetName]
    ) -> StrategySelection:
        preset, mult, reason = presets.select(regime, style, enabled_presets)
        return StrategySelection(regime=regime, style=style, preset=preset, size_multiplier=mult, reason=reason)

    # ------------------------------------------------------------------ manual
    def evaluate_manual(self, strategy: ManualStrategy, candles: Sequence[Candle], portfolio: Portfolio) -> Signal:
        """Evaluate the LAST candle: exits for open positions in ``strategy.symbol`` first (RSI/MACD on the close;
        SL/TP/trailing only if ``manual_price_exits``, otherwise RiskManager.check_exits owns them), otherwise the
        entry gate + hard filters + AND/OR conditions. Returns BUY / SELL / STAY_OUT with the reason and condition
        names. SELLs carry exit_reason, position_id and price = intended fill (contracts v0.2)."""
        label = manual_strategy_label(strategy)
        base = dict(symbol=strategy.symbol, source=AgentMode.MANUAL, strategy=label)
        cs, rep = sanitize_candles(candles)
        if not cs:
            return Signal(
                action=Action.STAY_OUT,
                conditions_failed=["insufficient_data"],
                reason="No valid candles available" + self._data_note(rep),
                **base,
            )
        note = self._data_note(rep)
        bar_s = TIMEFRAME_SECONDS[strategy.timeframe]
        i = len(cs) - 1
        c = cs[i]
        base["price"] = c.close
        mi = ManualIndicators.compute(strategy, cs)
        held = [p for p in portfolio.positions if p.symbol == strategy.symbol]

        exit_sig = self._manual_exit_signal(strategy, mi, cs, held, portfolio, base)
        if exit_sig is not None:
            return exit_sig

        r = strategy.risk
        if len(held) >= r.max_open_positions:
            return Signal(
                action=Action.STAY_OUT,
                conditions_failed=["max_open_positions"],
                reason=f"Max open positions reached ({len(held)}/{r.max_open_positions}); no exit triggered",
                **base,
            )
        since = portfolio.bars_since_exit.get(strategy.symbol)
        if since is not None and since <= r.cooldown_bars:
            return Signal(
                action=Action.STAY_OUT,
                conditions_failed=["cooldown"],
                reason=(
                    f"Cooldown: {since} {'bar' if since == 1 else 'bars'} since the last exit "
                    f"(needs more than {r.cooldown_bars})"
                ),
                **base,
            )
        if len(cs) < self.warmup_bars + 1:
            return Signal(
                action=Action.STAY_OUT,
                conditions_failed=["insufficient_data"],
                reason=f"Insufficient data: {len(cs)} valid candles, need at least {self.warmup_bars + 1} "
                "before entering (indicator warm-up)" + note,
                **base,
            )
        blocked = self._live_block(strategy.symbol, cs, bar_s, base)
        if blocked is not None:
            return blocked
        vol_ok = not self.live_safe or volume_applicable(strategy.symbol, self.live_config)
        f_ok, f_fails = check_filters(strategy, mi, cs, i, volume_applicable=vol_ok)
        if not f_ok:
            names = (["market_hours"] if any("market hours" in f for f in f_fails) else []) + (
                ["min_volume"] if any("min-volume" in f for f in f_fails) else []
            )
            return Signal(
                action=Action.STAY_OUT,
                conditions_failed=names,
                reason="Filter blocked entry: " + "; ".join(f_fails) + note,
                **base,
            )
        ev = evaluate_entry(strategy, mi, cs, i, volume_applicable=vol_ok)
        if not ev.met and not ev.failed:
            why = (
                "no entry conditions enabled"
                if not ev.skipped
                else "indicators still warming up: " + ", ".join(ev.skipped)
            )
            return Signal(action=Action.STAY_OUT, reason=f"No entry: {why}" + note, **base)
        logic = strategy.entry.logic.value
        detail = "; ".join(ev.detail)
        if self.live_safe and self.live_config.require_all_conditions_ready and ev.skipped:
            return Signal(
                action=Action.STAY_OUT,
                conditions_met=ev.met,
                conditions_failed=ev.failed + ["insufficient_data"],
                reason="Live safety: indicators still warming up for " + ", ".join(ev.skipped) + note,
                **base,
            )
        if not ev.ok:
            return Signal(
                action=Action.STAY_OUT,
                conditions_met=ev.met,
                conditions_failed=ev.failed,
                reason=f"Entry conditions not met ({logic}): {detail}" + note,
                **base,
            )
        size_pct, notional, size_note, blocked = self._size(
            strategy.symbol, r.position_size_pct, portfolio.equity, cs, base, ev.met, user_sized=True
        )
        if blocked is not None:
            return blocked
        return Signal(
            action=Action.BUY,
            conditions_met=ev.met,
            conditions_failed=ev.failed,
            reason=f"Entry conditions met ({logic}): {detail}" + size_note + note,
            proposed_size_pct=size_pct,
            proposed_notional=notional,
            **base,
        )

    def _manual_exit_signal(
        self,
        s: ManualStrategy,
        mi: ManualIndicators,
        cs: Sequence[Candle],
        held: list[Position],
        portfolio: Portfolio,
        base: dict,
    ) -> Optional[Signal]:
        """One SELL for the first (oldest) position that should exit, keyed by Position.id.
        Price exits (only if ``manual_price_exits``) follow ``exits`` exactly like Bot 3's check_exits, using
        Position.highest_price as the prior-bar high-water mark; then RSI / MACD exits on the close."""
        c = cs[-1]
        ordered = sorted(held, key=lambda p: p.opened_at)
        if self.manual_price_exits:
            for p in ordered:
                pct = p.trailing_stop_pct or (s.exit.trailing_stop.pct if s.exit.trailing_stop.enabled else None)
                trail = exits.trail_from_hwm(max(p.highest_price or 0.0, p.avg_entry_price), pct) if pct else None
                ml = exits.max_loss_level(
                    p.avg_entry_price, p.quantity, portfolio.starting_capital, s.risk.max_loss_per_trade_pct
                )
                pe = exits.check_price_exit(
                    c.open, c.high, c.low, p.avg_entry_price, s.exit.take_profit_pct, s.exit.stop_loss_pct,
                    trail, ml, stop_loss_price=p.stop_loss_price, take_profit_price=p.take_profit_price,
                )  # fmt: skip
                if pe is not None:
                    how = f"gapped through {pe.level:.6g}, fill at open" if pe.gap else "fill at level"
                    return self._sell(p, pe.reason, pe.price, f"{how} {pe.price:.6g}", base)
        ie = indicator_exit(s, mi, len(cs) - 1)
        if ie is not None and ordered:
            p = ordered[0]
            extra = f" ({len(ordered) - 1} more position(s) also qualify)" if len(ordered) > 1 else ""
            return self._sell(p, ie[0], c.close, f"{ie[1]}{extra}", base)
        return None

    @staticmethod
    def _sell(p: Position, reason: ExitReason, price: float, detail: str, base: dict) -> Signal:
        b = {**base, "price": price}
        return Signal(
            action=Action.SELL,
            reason=detail,
            conditions_met=[reason.value],
            exit_reason=reason,
            position_id=p.id,
            proposed_size_pct=100.0,
            proposed_notional=p.quantity * price,
            **b,
        )

    # ------------------------------------------------------------------ auto
    @staticmethod
    def _auto_basis(config: AutoConfig, portfolio: Portfolio) -> float:
        """Auto sizes from the capital allocated to it, but never from more than the portfolio actually has:
        min(config.capital, portfolio.equity). A 1000+ USDT allocation on a ~110 USDT wallet must not size 2% of
        1000+ (= the 25 USDT cap) per buy."""
        eq = portfolio.equity
        return min(config.capital, eq) if eq and eq > 0 else config.capital

    def evaluate_auto(self, config: AutoConfig, candles: Sequence[Candle], portfolio: Portfolio) -> Signal:
        """detect_regime -> select_strategy -> evaluate ONLY the selected preset."""
        base = dict(symbol=config.symbol, source=AgentMode.AUTO)
        cs, rep = sanitize_candles(candles)
        if not cs:
            return Signal(
                action=Action.STAY_OUT,
                strategy="NONE",
                conditions_failed=["insufficient_data"],
                reason="No valid candles available" + self._data_note(rep),
                **base,
            )
        bar_s = TIMEFRAME_SECONDS[config.timeframe]
        ra = _detect_regime(cs, config.timeframe)
        base.update(regime=ra.regime, price=cs[-1].close)
        held = [p for p in portfolio.positions if p.symbol == config.symbol]
        if ra.features.get("insufficient_data") and not held:
            return Signal(
                action=Action.STAY_OUT,
                strategy="NONE",
                conditions_failed=["insufficient_data"],
                reason=f"Regime unknown, staying out: {ra.reason}",
                **base,
            )
        sel = self.select_strategy(ra.regime, config.style, config.enabled_presets)
        prefix = f"Regime {ra.regime.value} ({ra.reason}); {sel.reason}"
        if sel.preset is None:
            return Signal(action=Action.STAY_OUT, strategy="NONE", reason=prefix, **base)
        d = presets.evaluate_preset(sel.preset, config.style, cs, holding=bool(held), bar_seconds=bar_s)
        if d.action == Action.BUY and ra.features.get("insufficient_data"):
            return Signal(
                action=Action.STAY_OUT,
                strategy=sel.preset.value,
                preset=sel.preset,
                conditions_failed=["insufficient_data"],
                reason=f"Regime unknown, no new entries (exits only): {ra.reason}",
                **base,
            )
        if d.action == Action.BUY:
            blocked = self._live_block(
                config.symbol, cs, bar_s, {**base, "strategy": sel.preset.value, "preset": sel.preset}, d.met
            )
            if blocked is not None:
                return blocked
        size_pct = 0.0
        notional = 0.0
        extra: dict = {}
        size_note = ""
        if d.action == Action.BUY:
            size_pct, notional, size_note, blocked = self._size(
                config.symbol,
                presets.BASE_SIZE_PCT[config.style] * sel.size_multiplier,
                self._auto_basis(config, portfolio),
                cs,
                {**base, "strategy": sel.preset.value, "preset": sel.preset},
                d.met,
            )
            if blocked is not None:
                return blocked
        elif d.action == Action.SELL:
            target = min(held, key=lambda p: p.opened_at)  # oldest position, keyed by id (one SELL per Signal)
            size_pct = 100.0
            notional = target.quantity * cs[-1].close
            extra["position_id"] = target.id
            extra["exit_reason"] = d.exit_reason
        return Signal(
            action=d.action,
            strategy=sel.preset.value,
            preset=sel.preset,
            reason=f"{prefix}. {d.reason}{size_note}",
            conditions_met=d.met,
            conditions_failed=d.failed,
            proposed_size_pct=size_pct,
            proposed_notional=notional,
            **extra,
            **base,
        )

    # ------------------------------------------------------------------ backtest
    def backtest_manual(
        self,
        strategy: ManualStrategy,
        candles: Sequence[Candle],
        initial_capital: float,
        fee_pct: Optional[float] = None,
        slippage_bps: Optional[float] = None,
    ) -> BacktestResult:
        return self.backtest_manual_detailed(strategy, candles, initial_capital, fee_pct, slippage_bps).result

    def backtest_manual_detailed(
        self,
        strategy: ManualStrategy,
        candles: Sequence[Candle],
        initial_capital: float,
        fee_pct: Optional[float] = None,
        slippage_bps: Optional[float] = None,
    ) -> BacktestRun:
        """Same as backtest_manual plus per-trade entry/exit explanations (``BacktestRun.notes``)."""
        return run_backtest(
            strategy,
            candles,
            initial_capital,
            fee_pct=self.fee_pct if fee_pct is None else fee_pct,
            slippage_bps=self.slippage_bps if slippage_bps is None else slippage_bps,
            warmup_bars=self.warmup_bars,
        )
