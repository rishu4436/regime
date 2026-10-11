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

    def __init__(
        self,
        fee_pct: float = 0.0,
        slippage_bps: float = 0.0,
        warmup_bars: int = SIM_WARMUP_BARS,
        manual_price_exits: bool = False,
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
        if not candles:
            return Signal(action=Action.STAY_OUT, reason="No candles available", **base)
        cs = sorted(candles, key=lambda c: c.timestamp)
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
        extra: dict = {}
        if d.action == Action.BUY:
            size_pct = presets.BASE_SIZE_PCT[config.style] * sel.size_multiplier
            notional = config.capital * size_pct / 100
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
            reason=f"{prefix}. {d.reason}",
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
