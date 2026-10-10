"""DefaultStrategyEngine - implements backend.contracts.StrategyEngine.

Pure: no I/O, no execution, no risk decisions.
"""

from __future__ import annotations

from typing import Optional, Sequence

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
)

from . import exits, presets
from .backtest import BacktestRun, run_backtest
from .manual import ManualIndicators, check_filters, evaluate_entry, indicator_exit
from .regime import detect_regime as _detect_regime


def manual_strategy_label(strategy: ManualStrategy) -> str:
    return f"MANUAL:{strategy.name}"


class DefaultStrategyEngine:
    """Stateless strategy & regime engine.

    ``fee_pct`` / ``slippage_bps`` are the defaults used by ``backtest_manual`` (0 = simulator parity);
    ``warmup_bars`` is the first bar index the backtest trades on (simulator: 60).
    """

    def __init__(self, fee_pct: float = 0.0, slippage_bps: float = 0.0, warmup_bars: int = SIM_WARMUP_BARS) -> None:
        if fee_pct < 0 or slippage_bps < 0 or warmup_bars < 0:
            raise ValueError("fee_pct, slippage_bps and warmup_bars must be >= 0")
        self.fee_pct = fee_pct
        self.slippage_bps = slippage_bps
        self.warmup_bars = warmup_bars

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
        """Evaluate the LAST candle: exits for open positions in ``strategy.symbol`` first (price exits per
        ``exits`` on the bar's OHLC, then RSI/MACD on the close), otherwise the entry gate + hard filters +
        AND/OR conditions. Returns BUY / SELL / STAY_OUT with the reason and condition names."""
        label = manual_strategy_label(strategy)
        base = dict(symbol=strategy.symbol, source=AgentMode.MANUAL, strategy=label)
        if not candles:
            return Signal(action=Action.STAY_OUT, reason="No candles available", **base)
        cs = sorted(candles, key=lambda c: c.timestamp)
        i = len(cs) - 1
        c = cs[i]
        base["price"] = c.close
        mi = ManualIndicators.compute(strategy, cs)
        held = [p for p in portfolio.positions if p.symbol == strategy.symbol]

        for p in held:
            hit = self._manual_exit(strategy, mi, cs, p, portfolio)
            if hit is not None:
                reason, detail = hit
                return Signal(
                    action=Action.SELL,
                    reason=f"Exit {reason.value}: {detail}",
                    conditions_met=[reason.value],
                    proposed_size_pct=100.0,
                    proposed_notional=p.quantity * c.close,
                    **base,
                )

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
                reason=f"Cooldown: {since} bars since last exit (need > {r.cooldown_bars})",
                **base,
            )
        f_ok, f_fails = check_filters(strategy, mi, cs, i)
        if not f_ok:
            names = (["market_hours"] if any("market hours" in f for f in f_fails) else []) + (
                ["min_volume"] if any("min-volume" in f for f in f_fails) else []
            )
            return Signal(
                action=Action.STAY_OUT,
                conditions_failed=names,
                reason="Filter blocked entry: " + "; ".join(f_fails),
                **base,
            )
        ev = evaluate_entry(strategy, mi, cs, i)
        if not ev.met and not ev.failed:
            why = (
                "no entry conditions enabled"
                if not ev.skipped
                else "indicators still warming up: " + ", ".join(ev.skipped)
            )
            return Signal(action=Action.STAY_OUT, reason=f"No entry: {why}", **base)
        logic = strategy.entry.logic.value
        detail = "; ".join(ev.detail)
        if not ev.ok:
            return Signal(
                action=Action.STAY_OUT,
                conditions_met=ev.met,
                conditions_failed=ev.failed,
                reason=f"Entry conditions not met ({logic}): {detail}",
                **base,
            )
        notional = portfolio.equity * r.position_size_pct / 100
        return Signal(
            action=Action.BUY,
            conditions_met=ev.met,
            conditions_failed=ev.failed,
            reason=f"Entry conditions met ({logic}): {detail}",
            proposed_size_pct=r.position_size_pct,
            proposed_notional=notional,
            **base,
        )

    @staticmethod
    def _manual_exit(
        s: ManualStrategy, mi: ManualIndicators, cs: Sequence[Candle], p: Position, portfolio: Portfolio
    ) -> Optional[tuple[ExitReason, str]]:
        c = cs[-1]
        trail = None
        if s.exit.trailing_stop.enabled:
            hwm = p.highest_price if p.highest_price is not None else p.avg_entry_price
            trail = max(
                exits.initial_trail(p.avg_entry_price, s.exit.trailing_stop.pct),
                hwm * (1 - s.exit.trailing_stop.pct / 100),
            )
        ml = exits.max_loss_level(p.avg_entry_price, p.quantity, portfolio.equity, s.risk.max_loss_per_trade_pct)
        pe = exits.check_price_exit(
            c.open, c.high, c.low, p.avg_entry_price, s.exit.take_profit_pct, s.exit.stop_loss_pct, trail, ml
        )
        if pe is not None:
            return pe.reason, f"{'gapped through' if pe.gap else 'hit'} level, fill ~{pe.price:.6g}"
        return indicator_exit(s, mi, len(cs) - 1)

    # ------------------------------------------------------------------ auto
    def evaluate_auto(self, config: AutoConfig, candles: Sequence[Candle], portfolio: Portfolio) -> Signal:
        """detect_regime -> select_strategy -> evaluate ONLY the selected preset."""
        base = dict(symbol=config.symbol, source=AgentMode.AUTO)
        if not candles:
            return Signal(action=Action.STAY_OUT, strategy="NONE", reason="No candles available", **base)
        cs = sorted(candles, key=lambda c: c.timestamp)
        ra = self.detect_regime(cs)
        sel = self.select_strategy(ra.regime, config.style, config.enabled_presets)
        base.update(regime=ra.regime, price=cs[-1].close)
        prefix = f"Regime {ra.regime.value} ({ra.reason}); {sel.reason}"
        if sel.preset is None:
            return Signal(action=Action.STAY_OUT, strategy="NONE", reason=prefix, **base)
        held = [p for p in portfolio.positions if p.symbol == config.symbol]
        d = presets.evaluate_preset(sel.preset, config.style, cs, holding=bool(held))
        size_pct = 0.0
        notional = 0.0
        if d.action == Action.BUY:
            size_pct = presets.BASE_SIZE_PCT[config.style] * sel.size_multiplier
            notional = config.capital * size_pct / 100
        elif d.action == Action.SELL:
            size_pct = 100.0
            notional = sum(p.quantity for p in held) * cs[-1].close
        return Signal(
            action=d.action,
            strategy=sel.preset.value,
            preset=sel.preset,
            reason=f"{prefix}. {d.reason}",
            conditions_met=d.met,
            conditions_failed=d.failed,
            proposed_size_pct=size_pct,
            proposed_notional=notional,
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
