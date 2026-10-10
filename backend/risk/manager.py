"""Bot 3 - Risk & Control layer. ``DefaultRiskManager`` implements ``backend.contracts.RiskManager``.

Deterministic, no network, no secrets. Inject ``clock`` in tests. See backend/risk/README.md for the full design.
"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Callable, Optional

from backend.contracts import (
    Action,
    ActivationMode,
    AgentMode,
    Candle,
    ControlState,
    ExitReason,
    Portfolio,
    Position,
    RiskCheck,
    RiskVerdict,
    Signal,
    TradingMode,
    utc_now,
)

from .config import RiskConfig, SymbolRiskRules
from .exits import Bar, ExitLevels, compute_levels, evaluate_bar, max_loss_level

# Exit reasons produced by the risk layer itself (protective exits that never wait for user confirmation).
RISK_EXIT_REASONS = frozenset({ExitReason.STOP_LOSS, ExitReason.TAKE_PROFIT, ExitReason.TRAILING_STOP,
                               ExitReason.MAX_LOSS, ExitReason.EMERGENCY_STOP})


def _fmt(x: float) -> str:
    return f"{x:,.2f}"


def is_risk_exit(signal: Signal) -> bool:
    """True for a protective SELL as produced by ``check_exits`` (exit_reason in RISK_EXIT_REASONS)."""
    return signal.action == Action.SELL and signal.exit_reason in RISK_EXIT_REASONS


class DefaultRiskManager:
    """Risk gate + exit enforcement + global controls. Thread-safe. The only in-memory state is the ControlState
    (and config); positions, trailing high-water marks (Position.highest_price), cooldowns and PnL are read from the
    Portfolio passed in, which is never mutated."""

    def __init__(
        self,
        control: ControlState | None = None,
        config: RiskConfig | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._clock = clock
        self._config = config or RiskConfig()
        self._control = control or ControlState(updated_at=clock())
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ configuration
    @property
    def config(self) -> RiskConfig:
        return self._config

    def set_config(self, config: RiskConfig) -> None:
        with self._lock:
            self._config = config

    def set_symbol_rules(self, symbol: str, rules: SymbolRiskRules | None) -> None:
        """Per-symbol override; ``None`` removes it."""
        with self._lock:
            sr = dict(self._config.symbol_rules)
            if rules is None:
                sr.pop(symbol, None)
            else:
                sr[symbol] = rules
            self._config = self._config.model_copy(update={"symbol_rules": sr})

    def apply_manual_strategy(self, strategy) -> None:  # strategy: ManualStrategy
        """Use a ManualStrategy's ``risk`` / ``exit`` / ``activation`` for its symbol."""
        self.set_symbol_rules(strategy.symbol, SymbolRiskRules.from_manual_strategy(strategy))

    # ------------------------------------------------------------------ controls
    def get_control_state(self) -> ControlState:
        with self._lock:
            return self._control

    def _update(self, **kw) -> ControlState:
        with self._lock:
            self._control = self._control.model_copy(update={**kw, "updated_at": self._clock()})
            return self._control

    def pause(self, reason: str = "") -> ControlState:
        return self._update(paused=True, reason=f"Paused: {reason}" if reason else "Paused by user")

    def resume(self) -> ControlState:
        """Clears pause. Does NOT clear an emergency stop (use ``reset_emergency_stop``)."""
        with self._lock:
            if self._control.emergency_stopped:
                return self._update(paused=False, reason=(
                    "Pause cleared, but EMERGENCY STOP is still active - call reset_emergency_stop() to trade again"))
            return self._update(paused=False, reason="Resumed")

    def emergency_stop(self, reason: str = "") -> ControlState:
        """Sets emergency_stopped=True (``paused`` untouched): every BUY is blocked and check_exits closes all positions."""
        return self._update(emergency_stopped=True,
                            reason=f"EMERGENCY STOP: {reason}" if reason else "EMERGENCY STOP by user")

    def reset_emergency_stop(self) -> ControlState:
        """Explicit user action: clears emergency_stopped and leaves ``paused`` unchanged."""
        with self._lock:
            paused = self._control.paused
            return self._update(emergency_stopped=False, reason=(
                "Emergency stop reset - agent is still paused, call resume() to trade" if paused
                else "Emergency stop reset - trading allowed"))

    def set_confirmation_mode(self, enabled: bool) -> ControlState:
        return self._update(confirmation_mode=bool(enabled),
                            reason="Confirmation mode ON: every entry waits for the user" if enabled
                            else "Confirmation mode OFF: approved entries auto-execute")

    def set_trading_mode(self, mode: TradingMode) -> ControlState:
        """Switch SIM/PAPER/LIVE. LIVE is refused (state unchanged) unless ``RiskConfig.allow_live``."""
        with self._lock:
            if mode == TradingMode.LIVE and not self._config.allow_live:
                return self._update(reason="LIVE trading refused: not enabled in risk config (allow_live=False)")
            return self._update(trading_mode=mode, reason=f"Trading mode set to {mode.value}")

    def _effective_control(self, control: ControlState | None) -> ControlState:
        """Most restrictive combination of the manager's own state and the one passed by the caller."""
        own = self.get_control_state()
        if control is None:
            return own
        live = TradingMode.LIVE in (own.trading_mode, control.trading_mode)
        return own.model_copy(update=dict(
            paused=own.paused or control.paused,
            emergency_stopped=own.emergency_stopped or control.emergency_stopped,
            confirmation_mode=own.confirmation_mode or control.confirmation_mode,
            trading_mode=TradingMode.LIVE if live else control.trading_mode,
            reason=control.reason or own.reason,
        ))

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _position_value(p: Position, price: Optional[float] = None) -> float:
        return p.quantity * (price or p.current_price or p.avg_entry_price)

    @staticmethod
    def _equity(portfolio: Portfolio) -> float:
        return portfolio.equity if portfolio.equity > 0 else 0.0

    @staticmethod
    def _hwm_for(p: Position) -> float:
        """High-water mark from PRIOR bars only: Position.highest_price as given (the app raises it from bar.high
        after check_exits), never below the entry price."""
        return max(p.highest_price or 0.0, p.avg_entry_price)

    def exit_levels(self, position: Position, portfolio: Portfolio | None = None) -> ExitLevels:
        """Stop / target / trailing / max-loss levels currently in force for a position (for the UI)."""
        prules, erules, _ = self._config.rules_for(position.symbol)
        capital = portfolio.starting_capital if portfolio else 0.0
        return compute_levels(position, erules, self._hwm_for(position),
                              max_loss_level(position, prules.max_loss_per_trade_pct, capital))

    # ------------------------------------------------------------------ check
    def check(self, signal: Signal, portfolio: Portfolio, control: ControlState) -> RiskVerdict:
        with self._lock:
            ctl = self._effective_control(control)
            if signal.action == Action.STAY_OUT:
                return self._verdict(signal, False, [RiskCheck(
                    name="signal_action", passed=False,
                    detail=f"Signal is STAY_OUT - nothing to execute ({signal.reason})")], 0.0, 0.0, False)
            if signal.action == Action.SELL:
                return self._check_sell(signal, portfolio, ctl)
            return self._check_buy(signal, portfolio, ctl)

    def _mode_check(self, ctl: ControlState) -> RiskCheck:
        if ctl.trading_mode == TradingMode.LIVE and not self._config.allow_live:
            return RiskCheck(name="trading_mode", passed=False,
                             detail="LIVE trading is disabled in the risk config (allow_live=False)")
        return RiskCheck(name="trading_mode", passed=True, detail=f"Trading mode {ctl.trading_mode.value} allowed")

    def _check_buy(self, signal: Signal, portfolio: Portfolio, ctl: ControlState) -> RiskVerdict:
        cfg = self._config
        prules, erules, activation = cfg.rules_for(signal.symbol)
        checks: list[RiskCheck] = [RiskCheck(name="signal_action", passed=True, detail=f"BUY {signal.symbol} proposed by {signal.strategy}")]
        checks.append(self._mode_check(ctl))
        checks.append(RiskCheck(name="emergency_stop", passed=not ctl.emergency_stopped,
                                detail="Emergency stop is active - all new entries are blocked" if ctl.emergency_stopped
                                else "No emergency stop"))
        checks.append(RiskCheck(name="paused", passed=not ctl.paused,
                                detail="Agent is paused - new entries are blocked" if ctl.paused else "Agent is running"))

        n_open = len(portfolio.positions)
        checks.append(RiskCheck(
            name="max_open_positions", passed=n_open < prules.max_open_positions,
            detail=f"{n_open} open position(s), limit {prules.max_open_positions}"
            + ("" if n_open < prules.max_open_positions else " - limit reached")))

        bse = portfolio.bars_since_exit.get(signal.symbol)
        if bse is None:
            checks.append(RiskCheck(name="cooldown", passed=True, detail=f"No recent exit on {signal.symbol}"))
        else:
            ok = bse > prules.cooldown_bars
            checks.append(RiskCheck(name="cooldown", passed=ok, detail=(
                f"{bse} bar(s) since last exit on {signal.symbol}, cooldown {prules.cooldown_bars}"
                + ("" if ok else f" - wait {prules.cooldown_bars - bse + 1} more bar(s)"))))

        if cfg.daily_loss_limit_pct is None:
            checks.append(RiskCheck(name="daily_loss_limit", passed=True, detail="Daily loss limit off"))
        else:
            limit = portfolio.starting_capital * cfg.daily_loss_limit_pct / 100.0
            ok = portfolio.daily_pnl > -limit
            checks.append(RiskCheck(name="daily_loss_limit", passed=ok, detail=(
                f"Today's PnL {_fmt(portfolio.daily_pnl)} USDT vs limit -{_fmt(limit)} USDT ({cfg.daily_loss_limit_pct}% of capital)"
                + ("" if ok else " - daily loss limit hit, no new entries today"))))

        if cfg.max_trades_per_day is None:
            checks.append(RiskCheck(name="max_trades_per_day", passed=True, detail="Daily trade limit off"))
        else:
            ok = portfolio.trades_today < cfg.max_trades_per_day
            checks.append(RiskCheck(name="max_trades_per_day", passed=ok,
                                    detail=f"{portfolio.trades_today} trade(s) today, limit {cfg.max_trades_per_day}"))

        # ---- sizing (all reductions recorded; failure only if the order ends up too small)
        equity = self._equity(portfolio)
        gates_ok = all(c.passed for c in checks)
        notional = 0.0
        if equity <= 0:
            checks.append(RiskCheck(name="position_size", passed=False, detail="Portfolio equity is zero - cannot size an order"))
        else:
            base_pct = prules.position_size_pct
            want_pct = base_pct
            src = f"rule {base_pct}% of equity"
            if signal.proposed_notional > 0:
                want_pct = signal.proposed_notional / equity * 100.0
                src = f"signal asked {_fmt(signal.proposed_notional)} USDT ({want_pct:.2f}%)"
            elif signal.proposed_size_pct > 0:
                want_pct = signal.proposed_size_pct
                src = f"signal asked {want_pct}%"
            pct = min(want_pct, base_pct)
            notional = equity * pct / 100.0
            checks.append(RiskCheck(name="position_size", passed=True, detail=(
                f"{src}; size {pct:.2f}% of equity {_fmt(equity)} = {_fmt(notional)} USDT"
                + (f" (capped at max {base_pct}%)" if want_pct > base_pct else ""))))

            def cap(name: str, limit: float, what: str) -> None:
                nonlocal notional
                if notional > limit:
                    checks.append(RiskCheck(name=name, passed=True,
                                            detail=f"{what}: reduced from {_fmt(notional)} to {_fmt(max(limit, 0))} USDT"))
                    notional = max(limit, 0.0)
                else:
                    checks.append(RiskCheck(name=name, passed=True, detail=f"{what}: {_fmt(notional)} USDT fits"))

            cap("cash_available", portfolio.cash, f"Cash available {_fmt(portfolio.cash)} USDT")
            token_exp = sum(self._position_value(p) for p in portfolio.positions if p.symbol == signal.symbol)
            token_lim = equity * cfg.max_token_exposure_pct / 100.0
            cap("max_token_exposure", token_lim - token_exp,
                f"{signal.symbol} exposure {_fmt(token_exp)} USDT, limit {cfg.max_token_exposure_pct}% = {_fmt(token_lim)} USDT")
            total_exp = sum(self._position_value(p) for p in portfolio.positions)
            total_lim = equity * cfg.max_total_exposure_pct / 100.0
            cap("max_total_exposure", total_lim - total_exp,
                f"Total exposure {_fmt(total_exp)} USDT, limit {cfg.max_total_exposure_pct}% = {_fmt(total_lim)} USDT")

            if prules.max_loss_per_trade_pct:
                max_loss = portfolio.starting_capital * prules.max_loss_per_trade_pct / 100.0
                sl = erules.stop_loss_pct / 100.0
                cap("max_loss_per_trade", max_loss / sl,
                    f"Max loss per trade {prules.max_loss_per_trade_pct}% = {_fmt(max_loss)} USDT at a {erules.stop_loss_pct}% stop")
            else:
                checks.append(RiskCheck(name="max_loss_per_trade", passed=True, detail="Max loss per trade off"))

            ok = notional >= max(cfg.min_order_notional, 1e-9)
            checks.append(RiskCheck(name="min_order_notional", passed=ok, detail=(
                f"Order {_fmt(notional)} USDT, minimum {_fmt(cfg.min_order_notional)} USDT" + ("" if ok else " - too small"))))

        approved = gates_ok and all(c.passed for c in checks)
        needs_conf = ctl.confirmation_mode or activation == ActivationMode.CONFIRMATION
        if approved:
            checks.append(RiskCheck(name="confirmation", passed=True, detail=(
                "Confirmation mode: waiting for the user to approve this order" if needs_conf
                else "Auto-execute: no confirmation needed")))
        if not approved:
            notional = 0.0
        size_pct = min(100.0, notional / equity * 100.0) if equity > 0 else 0.0
        return self._verdict(signal, approved, checks, size_pct, notional, approved and needs_conf)

    def _check_sell(self, signal: Signal, portfolio: Portfolio, ctl: ControlState) -> RiskVerdict:
        cfg = self._config
        risk_exit = is_risk_exit(signal)
        checks = [RiskCheck(name="signal_action", passed=True, detail=(
            f"SELL {signal.symbol}" + (" (protective exit from risk layer)" if risk_exit else f" proposed by {signal.strategy}")))]
        checks.append(self._mode_check(ctl))
        checks.append(RiskCheck(name="emergency_stop", passed=True, detail=(
            "Emergency stop active - closing positions is allowed" if ctl.emergency_stopped else "No emergency stop")))
        if ctl.paused and not (cfg.allow_exits_when_paused or ctl.emergency_stopped):
            checks.append(RiskCheck(name="paused", passed=False, detail="Agent is paused - exits are blocked by config"))
        else:
            checks.append(RiskCheck(name="paused", passed=True, detail=(
                "Agent is paused - protective exits still allowed" if ctl.paused else "Agent is running")))
        held = [p for p in portfolio.positions if p.symbol == signal.symbol
                and (signal.position_id is None or p.id == signal.position_id)]
        value = sum(self._position_value(p, signal.price) for p in held)
        target = f"position {signal.position_id} ({signal.symbol})" if signal.position_id else signal.symbol
        checks.append(RiskCheck(name="open_position", passed=bool(held), detail=(
            f"Holding {sum(p.quantity for p in held):g} {signal.symbol} worth {_fmt(value)} USDT in {target}" if held
            else f"No open {target} - nothing to sell")))
        notional = 0.0
        if held:
            notional = min(signal.proposed_notional, value) if signal.proposed_notional > 0 else value
            checks.append(RiskCheck(name="sell_size", passed=True, detail=(
                f"Sell {_fmt(notional)} USDT of {_fmt(value)} USDT" + (" (full position)" if notional >= value else ""))))
        approved = all(c.passed for c in checks)
        needs_conf = False
        if approved:
            _, _, activation = cfg.rules_for(signal.symbol)
            wants_conf = ctl.confirmation_mode or activation == ActivationMode.CONFIRMATION
            bypass = (risk_exit and cfg.risk_exits_bypass_confirmation) or ctl.emergency_stopped
            needs_conf = wants_conf and not bypass
            checks.append(RiskCheck(name="confirmation", passed=True, detail=(
                "Confirmation mode: waiting for the user to approve this sell" if needs_conf
                else "Protective exit - executes without confirmation" if wants_conf
                else "Auto-execute: no confirmation needed")))
        else:
            notional = 0.0
        equity = self._equity(portfolio)
        size_pct = min(100.0, notional / equity * 100.0) if equity > 0 else 0.0
        return self._verdict(signal, approved, checks, size_pct, notional, needs_conf)

    def _verdict(self, signal: Signal, approved: bool, checks: list[RiskCheck], size_pct: float, notional: float,
                 requires_confirmation: bool) -> RiskVerdict:
        if approved:
            verb = signal.action.value
            reasons = [f"Approved {verb} {signal.symbol} for {_fmt(notional)} USDT ({size_pct:.2f}% of equity)"
                       + (" - awaiting user confirmation" if requires_confirmation else "")]
            reasons += [c.detail for c in checks if "reduced" in c.detail]
        else:
            reasons = [f"Blocked ({c.name}): {c.detail}" for c in checks if not c.passed]
        return RiskVerdict(signal_id=signal.id, approved=approved, reasons=reasons, checks=checks,
                           adjusted_size_pct=round(size_pct, 6), adjusted_notional=round(notional, 8),
                           requires_confirmation=requires_confirmation, timestamp=self._clock())

    # ------------------------------------------------------------------ exits
    def check_exits(
        self, portfolio: Portfolio, prices: dict[str, float], bars: Optional[dict[str, Candle]] = None
    ) -> list[Signal]:
        """SL / TP / trailing / max-loss / emergency enforcement -> SELL signals (one per position).

        ``bars`` (symbol -> latest closed bar) enables the intrabar rules (low/high hit tests, SL wins if both hit,
        fill at the level or at the open on a gap). Symbols without a bar fall back to ``prices`` (degenerate bar,
        open=high=low=close=price), then to ``Position.current_price``. The portfolio is never mutated."""
        bars = bars or {}
        out: list[Signal] = []
        with self._lock:
            ctl = self._control
            cfg = self._config
        for p in portfolio.positions:
            candle = bars.get(p.symbol)
            if candle is not None:
                bar, ts = Bar.from_candle(candle), candle.timestamp
            else:
                px = prices.get(p.symbol) or p.current_price
                bar, ts = (Bar.from_price(px) if px and px > 0 else None), self._clock()
            if ctl.emergency_stopped:
                price = bar.close if bar else (p.current_price or p.avg_entry_price)
                pnl = (price - p.avg_entry_price) / p.avg_entry_price * 100.0
                out.append(self._exit_signal(p, ExitReason.EMERGENCY_STOP, price, ts,
                                             f"EMERGENCY_STOP: closing {p.symbol} at market (~{price:.4f}, PnL {pnl:+.2f}%)"
                                             + (f" - {ctl.reason}" if ctl.reason else "")))
                continue
            if bar is None:
                continue
            prules, erules, _ = cfg.rules_for(p.symbol)
            levels = compute_levels(p, erules, self._hwm_for(p),
                                    max_loss_level(p, prules.max_loss_per_trade_pct, portfolio.starting_capital))
            decision, _ = evaluate_bar(levels, bar)
            if decision is not None:
                out.append(self._exit_signal(p, decision.reason, decision.fill_price, ts, decision.detail))
        return out

    def check_exits_candles(self, portfolio: Portfolio, candles: dict[str, Candle]) -> list[Signal]:
        """Deprecated alias of ``check_exits(portfolio, {}, bars=candles)``."""
        return self.check_exits(portfolio, {}, bars=dict(candles))

    @staticmethod
    def _exit_signal(p: Position, reason: ExitReason, price: float, ts: datetime, detail: str) -> Signal:
        source = AgentMode.MANUAL if p.strategy.upper().startswith("MANUAL") else AgentMode.AUTO
        return Signal(symbol=p.symbol, action=Action.SELL, source=source, strategy=p.strategy, reason=detail,
                      conditions_met=[reason.value], price=price, exit_reason=reason, position_id=p.id,
                      proposed_notional=round(p.quantity * price, 8), timestamp=ts)


__all__ = ["DefaultRiskManager", "RISK_EXIT_REASONS", "is_risk_exit"]
