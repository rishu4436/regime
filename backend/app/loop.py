"""The trading loop: control -> (protective exits) -> regime -> strategy -> risk -> execute/confirm -> explain -> log.

Built only against the contract Protocols (engine / risk / exchange / explainer). One active agent at a time:
either a ManualStrategy or an AutoConfig. `tick()` runs exactly one cycle on the next bar; the API runs it on a
timer (`/api/loop/start`) or on demand (`/api/loop/tick`, used by the e2e tests).
"""
from __future__ import annotations

import inspect
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from backend.contracts import (
    Action, ActivationMode, AgentMode, AutoConfig, Candle, ControlState, Decision, EventType, ExecutionResult,
    ExitReason, ExitRules, ManualStrategy, OrderRequest, OrderSide, OrderStatus, Regime, RiskVerdict, Signal,
    StatusSnapshot, Timeframe, TradingMode, utc_now,
)
from backend.monitoring import AlertLevel, AlertManager, EventLog, PerformanceTracker

from .events import EventBus
from .exits import check_protective_exit, exit_reason_of, trailing_level
from .feed import PollingFeed, ReplayFeed
from .fallbacks import DAILY_LOSS_LIMIT_PCT
from .portfolio import Lot, PortfolioBook
from .settings import Settings
from .wiring import Components


class AgentStateError(RuntimeError):
    """Invalid state for the request (mapped to HTTP 409)."""


def call_check_exits(risk: Any, portfolio, prices: dict[str, float], bars: dict[str, Candle]) -> list[Signal]:
    """RiskManager.check_exits with the bar OHLC when the implementation accepts it (proposed contract param `bars`)."""
    fn = risk.check_exits
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        params = {}
    if "bars" in params or any(p.kind == p.VAR_KEYWORD for p in params.values()):
        return list(fn(portfolio, prices, bars=bars))
    return list(fn(portfolio, prices))


@dataclass
class Pending:
    decision: Decision
    signal: Signal
    verdict: RiskVerdict
    created: float = field(default_factory=time.monotonic)


class TradingLoop:
    def __init__(self, settings: Settings, comps: Components, bus: Optional[EventBus] = None) -> None:
        self.s, self.c = settings, comps
        self.bus = bus or EventBus(EventLog())
        self.alerts = AlertManager()
        self.perf = PerformanceTracker()
        self.book = PortfolioBook(settings.starting_capital, settings.trading_mode)
        self.sim = settings.trading_mode == TradingMode.SIM
        self.feed = ReplayFeed(comps.exchange, settings.sim_replay_bars, settings.window_bars) if self.sim else PollingFeed(comps.exchange, settings.window_bars)
        self.lock = threading.RLock()
        self.manual: Optional[ManualStrategy] = None
        self.auto: Optional[AutoConfig] = None
        self.current_regime: Optional[Regime] = None
        self.active_strategy: Optional[str] = None
        self.decisions: deque[Decision] = deque(maxlen=2000)
        self.pending: dict[str, Pending] = {}
        self.ticks = 0
        self.finished = False
        self._bar: Optional[Candle] = None
        self.perf.record_equity(utc_now() if not self.sim else self.book.now, self.book.starting_capital)
        for k, v in comps.names().items():
            if v.startswith("fallback:"):
                self._alert(AlertLevel.INFO, "COMPONENT_FALLBACK", f"{k}: using {v} until the real module is implemented")
        if settings.trading_mode == TradingMode.LIVE:
            self._alert(AlertLevel.CRITICAL, "LIVE_MODE", "LIVE trading enabled: real orders will be placed")

    # ------------------------------------------------------------------ helpers
    def _alert(self, level: AlertLevel, code: str, msg: str, symbol: Optional[str] = None) -> None:
        a = self.alerts.raise_(level, code, msg, symbol)
        self.bus.publish(EventType.ALERT, a)

    def _publish_alerts(self, alerts: list) -> None:
        for a in alerts:
            self.bus.publish(EventType.ALERT, a)

    @property
    def agent_mode(self) -> Optional[AgentMode]:
        return AgentMode.MANUAL if self.manual else AgentMode.AUTO if self.auto else None

    @property
    def symbol(self) -> Optional[str]:
        return self.manual.symbol if self.manual else self.auto.symbol if self.auto else None

    @property
    def timeframe(self) -> Timeframe:
        return self.manual.timeframe if self.manual else self.auto.timeframe if self.auto else Timeframe.H1

    def _now(self) -> datetime:
        return self._bar.timestamp if (self.sim and self._bar) else utc_now()

    def _exit_rules(self) -> ExitRules:
        return self.manual.exit if self.manual else ExitRules()

    def control(self) -> ControlState:
        return self.c.risk.get_control_state()

    # ------------------------------------------------------------------ activation
    def _check_switch(self, symbol: str) -> None:
        others = {l.position.symbol for l in self.book.lots if l.position.symbol != symbol}
        if others:
            raise AgentStateError(f"close open positions in {', '.join(sorted(others))} before switching symbol")

    def activate_manual(self, strategy: ManualStrategy) -> StatusSnapshot:
        with self.lock:
            self._check_switch(strategy.symbol)
            self.manual, self.auto = strategy, None
            self.active_strategy = f"MANUAL:{strategy.name}"
            self._on_activate(strategy.symbol, strategy.timeframe, strategy.activation)
            return self.status()

    def activate_auto(self, config: AutoConfig) -> StatusSnapshot:
        with self.lock:
            self._check_switch(config.symbol)
            self.manual, self.auto = None, config
            self.active_strategy = None
            self._on_activate(config.symbol, config.timeframe, config.activation)
            return self.status()

    def _on_activate(self, symbol: str, tf: Timeframe, activation: ActivationMode) -> None:
        self.finished = False
        self.current_regime = None
        ctrl = self.c.risk.set_confirmation_mode(activation == ActivationMode.CONFIRMATION)
        self.bus.publish(EventType.CONTROL, ctrl)
        self.bus.publish(EventType.STATUS, self.status())

    def deactivate(self) -> StatusSnapshot:
        with self.lock:
            self.manual = self.auto = None
            self.active_strategy = None
            self.pending.clear()
            st = self.status()
            self.bus.publish(EventType.STATUS, st)
            return st

    # ------------------------------------------------------------------ the cycle
    def tick(self) -> list[Decision]:
        with self.lock:
            sym = self.symbol
            if sym is None:
                raise AgentStateError("no active agent: activate a manual strategy or auto config first")
            window = self.feed.next_window(sym, self.timeframe)
            if window is None:
                if self.sim and not self.finished:
                    self.finished = True
                    self._alert(AlertLevel.INFO, "REPLAY_FINISHED", f"SIM replay of {sym} reached the last bar", sym)
                return []
            self.ticks += 1
            bar = self._bar = window[-1]
            now = self._now()
            self._set_sim_price(sym, bar.close)
            self.book.on_bar(sym, bar, now)
            self.bus.publish(EventType.PRICE, {"symbol": sym, "price": bar.close, "candle": bar.model_dump(mode="json")})
            out: list[Decision] = []

            # 1) protective exits (SL / TP / trailing) - always, even when paused; never need confirmation
            regime = self._detect(window)
            sigs = call_check_exits(self.c.risk, self.book.portfolio(), {sym: bar.close}, {sym: bar})
            used: set[int] = set()
            for sig in sigs:
                if sig.action == Action.SELL:
                    lot = self._match_lot(sig, bar, used)
                    if lot is not None:
                        used.add(id(lot))
                        out.append(self._exit([lot], sig, exit_reason_of(sig), regime, verdict=None))
            self.book.raise_highs(sym, bar.high)

            # 2) control
            control = self.control()
            if control.emergency_stopped:
                out.append(self._finalize(Decision(
                    mode=self.s.trading_mode, agent_mode=self.agent_mode, symbol=sym, regime=regime, timestamp=now,
                    strategy=self.active_strategy or "NONE", action=Action.STAY_OUT,
                    reason=f"emergency stop active: {control.reason or ''}".strip())))
                return self._end_tick(out)

            # 3) strategy (engine) -> 4) risk -> 5) execute / confirm
            pf = self.book.portfolio()
            if self.manual:
                signal = self.c.engine.evaluate_manual(self.manual, window, pf)
            else:
                signal = self.c.engine.evaluate_auto(self.auto, window, pf)
                self.active_strategy = signal.strategy if signal.preset else "NONE"
                if signal.regime is not None:
                    regime = self.current_regime = signal.regime
            base = dict(mode=self.s.trading_mode, agent_mode=self.agent_mode, symbol=sym, regime=regime, timestamp=now,
                        strategy=signal.strategy, signal=signal)
            if signal.action == Action.STAY_OUT:
                out.append(self._finalize(Decision(action=Action.STAY_OUT, reason=signal.reason, **base)))
                return self._end_tick(out)
            verdict = self.c.risk.check(signal, pf, control)
            if not verdict.approved:
                out.append(self._finalize(Decision(action=Action.STAY_OUT, risk_verdict=verdict,
                                                   reason=f"{signal.action.value} blocked by risk: " + "; ".join(verdict.reasons), **base)))
            elif signal.action == Action.SELL:
                lots = self.book.lots_for(sym)
                out.append(self._exit(lots, signal, exit_reason_of(signal), regime, verdict))
            elif verdict.requires_confirmation:
                d = self._pending_decision(signal, verdict, base)
                out.append(d)
            else:
                out.append(self._buy(signal, verdict, base))
            return self._end_tick(out)

    def _end_tick(self, out: list[Decision]) -> list[Decision]:
        pf = self.book.portfolio()
        self.perf.record_equity(self._now(), pf.equity)
        self.bus.publish(EventType.STATUS, self.status())
        return out

    def _detect(self, window: list[Candle]) -> Optional[Regime]:
        try:
            self.current_regime = self.c.engine.detect_regime(window).regime
        except Exception:
            pass
        return self.current_regime

    def _set_sim_price(self, sym: str, price: float) -> None:
        if self.sim and hasattr(self.c.exchange, "set_price"):
            self.c.exchange.set_price(sym, price)

    def _match_lot(self, sig: Signal, bar: Candle, used: set[int]) -> Optional[Lot]:
        lots = [l for l in self.book.lots_for(sig.symbol) if id(l) not in used]
        if len(lots) <= 1:
            return lots[0] if lots else None
        want = exit_reason_of(sig)
        for l in lots:
            p = l.position
            hit = check_protective_exit(open_=bar.open, high=bar.high, low=bar.low, stop_loss_price=p.stop_loss_price,
                                        take_profit_price=p.take_profit_price,
                                        trail_level=trailing_level(p.highest_price or p.avg_entry_price, p.trailing_stop_pct))
            if hit and (want is None or hit.reason == want) and (sig.price is None or abs(hit.price - sig.price) < 1e-9 * max(1, hit.price)):
                return l
        return lots[0]

    # ------------------------------------------------------------------ execution
    def _execute(self, side: OrderSide, sym: str, notional: float, decision_id: str, quantity: Optional[float] = None,
                 fill_price: Optional[float] = None) -> ExecutionResult:
        ex = self.c.exchange
        mode = self.s.trading_mode
        if ex.mode != mode:  # hard SIM/PAPER/LIVE separation
            res = ExecutionResult(client_order_id=decision_id, symbol=sym, side=side, status=OrderStatus.REJECTED,
                                  path=OrderRequest.model_fields["path"].default, mode=mode, error="exchange mode mismatch")
        else:
            try:
                if fill_price:
                    self._set_sim_price(sym, fill_price)  # SIM: protective exits fill at the stop / target / gap open
                quote = ex.get_quote(sym, side, notional)
                order = OrderRequest(symbol=sym, side=side, notional=quote.notional, quantity=quantity, quote_id=quote.quote_id,
                                     path=quote.path, mode=mode, decision_id=decision_id)
                res = ex.execute_swap(order, quote)
            except Exception as e:  # never let an exchange error kill the loop
                res = ExecutionResult(client_order_id=decision_id, symbol=sym, side=side, status=OrderStatus.FAILED,
                                      path=OrderRequest.model_fields["path"].default, mode=mode, error=f"{type(e).__name__}: {e}")
            finally:
                if fill_price and self._bar is not None:
                    self._set_sim_price(sym, self._bar.close)
        self.bus.publish(EventType.EXECUTION, res, decision_id=decision_id)
        self._publish_alerts(self.alerts.on_execution(res))
        return res

    def _buy(self, signal: Signal, verdict: RiskVerdict, base: dict, decision_id: Optional[str] = None) -> Decision:
        did = decision_id or Decision.model_fields["id"].default_factory()
        notional = verdict.adjusted_notional or signal.proposed_notional
        if notional <= 0:
            return self._finalize(Decision(id=did, action=Action.STAY_OUT, risk_verdict=verdict, reason="BUY approved with zero size", **base))
        res = self._execute(OrderSide.BUY, signal.symbol, notional, did)
        if res.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED) and res.filled_quantity > 0:
            spent = notional if res.status == OrderStatus.FILLED else res.notional + res.fee
            self.book.open_lot(res, spent, signal.strategy, self._exit_rules(), self._now())
            return self._finalize(Decision(id=did, action=Action.BUY, risk_verdict=verdict, execution=res,
                                           reason=signal.reason, **base))
        return self._finalize(Decision(id=did, action=Action.STAY_OUT, risk_verdict=verdict, execution=res,
                                       reason=f"BUY not filled ({res.status.value}{': ' + res.error if res.error else ''}); {signal.reason}", **base))

    def _exit(self, lots: list[Lot], signal: Signal, reason: Optional[ExitReason], regime: Optional[Regime],
              verdict: Optional[RiskVerdict]) -> Decision:
        sym = signal.symbol
        did = Decision.model_fields["id"].default_factory()
        strategy = lots[0].position.strategy if lots else signal.strategy
        base = dict(mode=self.s.trading_mode, agent_mode=self.agent_mode or signal.source, symbol=sym, regime=regime,
                    timestamp=self._now(), strategy=strategy, signal=signal, risk_verdict=verdict)
        if not lots:
            return self._finalize(Decision(id=did, action=Action.STAY_OUT, reason=f"SELL ignored: no open position in {sym}", **base))
        last: Optional[ExecutionResult] = None
        filled = 0
        for lot in list(lots):
            q = lot.position.quantity
            px = signal.price or self.book.prices.get(sym) or lot.position.avg_entry_price
            res = last = self._execute(OrderSide.SELL, sym, q * px, did, quantity=q, fill_price=signal.price)
            if res.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED) and res.filled_quantity > 0:
                filled += 1
                self.book.close_lot(lot, res, reason.value if reason else None, self._now())
                self.perf.record_trade(self.book.closed[-1].pnl)
                limit = self.book.starting_capital * DAILY_LOSS_LIMIT_PCT / 100
                self._publish_alerts(self.alerts.on_trade_closed(self.perf.consecutive_losses, self.book.daily_pnl, limit, sym))
        tag = reason.value if reason else "EXIT"
        if filled:
            return self._finalize(Decision(id=did, action=Action.SELL, execution=last, reason=f"{tag}: {signal.reason}", **base))
        return self._finalize(Decision(id=did, action=Action.STAY_OUT, execution=last,
                                       reason=f"{tag} exit not filled ({last.status.value if last else '-'}), retrying next bar", **base))

    def _pending_decision(self, signal: Signal, verdict: RiskVerdict, base: dict) -> Decision:
        did = Decision.model_fields["id"].default_factory()
        ex = ExecutionResult(client_order_id=did, symbol=signal.symbol, side=OrderSide.BUY, status=OrderStatus.PENDING_CONFIRMATION,
                             path=OrderRequest.model_fields["path"].default, mode=self.s.trading_mode,
                             notional=verdict.adjusted_notional or signal.proposed_notional, timestamp=self._now())
        d = self._finalize(Decision(id=did, action=Action.BUY, risk_verdict=verdict, execution=ex,
                                    reason=f"awaiting user confirmation: {signal.reason}", **base))
        self.pending[did] = Pending(d, signal, verdict)
        return d

    # ------------------------------------------------------------------ confirmations / overrides
    def pending_decisions(self) -> list[Decision]:
        with self.lock:
            return [p.decision for p in self.pending.values()]

    def approve(self, decision_id: str) -> Decision:
        with self.lock:
            p = self.pending.pop(decision_id, None)
            if p is None:
                raise KeyError(decision_id)
            d0 = p.decision
            base = dict(mode=d0.mode, agent_mode=d0.agent_mode, symbol=d0.symbol, regime=d0.regime, timestamp=self._now(),
                        strategy=d0.strategy, signal=p.signal)
            if time.monotonic() - p.created > self.s.pending_ttl_seconds:
                ex = d0.execution.model_copy(update={"status": OrderStatus.EXPIRED, "error": "confirmation window expired"})
                self._publish_alerts(self.alerts.on_execution(ex))
                return self._finalize(Decision(id=decision_id, action=Action.STAY_OUT, risk_verdict=p.verdict, execution=ex,
                                               reason="confirmation expired; not executed", **base))
            control = self.control()
            verdict = self.c.risk.check(p.signal, self.book.portfolio(), control)  # re-check at approval time
            if not verdict.approved:
                return self._finalize(Decision(id=decision_id, action=Action.STAY_OUT, risk_verdict=verdict,
                                               reason="approved by user but blocked by risk: " + "; ".join(verdict.reasons), **base))
            d = self._buy(p.signal, verdict, base, decision_id=decision_id)
            self.bus.publish(EventType.STATUS, self.status())
            return d

    def reject(self, decision_id: str, reason: str = "") -> Decision:
        with self.lock:
            p = self.pending.pop(decision_id, None)
            if p is None:
                raise KeyError(decision_id)
            d0 = p.decision
            ex = d0.execution.model_copy(update={"status": OrderStatus.REJECTED, "error": reason or "rejected by user"})
            return self._finalize(d0.model_copy(update={"action": Action.STAY_OUT, "execution": ex, "explanation": None,
                                                        "reason": f"rejected by user{': ' + reason if reason else ''}"}))

    def close_symbol(self, symbol: str, reason: ExitReason = ExitReason.MANUAL) -> Decision:
        with self.lock:
            lots = self.book.lots_for(symbol)
            if not lots:
                raise KeyError(symbol)
            px = self.book.prices.get(symbol) or self.c.exchange.get_price(symbol)
            sig = Signal(symbol=symbol, action=Action.SELL, source=self.agent_mode or AgentMode.MANUAL,
                         strategy=lots[0].position.strategy, reason=f"{reason.value} close requested", price=px,
                         conditions_met=[f"exit:{reason.value}"], timestamp=self._now())
            d = self._exit(lots, sig, reason, self.current_regime, None)
            self.bus.publish(EventType.STATUS, self.status())
            return d

    # ------------------------------------------------------------------ control
    def pause(self, reason: str = "") -> ControlState:
        with self.lock:
            return self._control_changed(self.c.risk.pause(reason))

    def resume(self) -> ControlState:
        with self.lock:
            return self._control_changed(self.c.risk.resume())

    def set_confirmation_mode(self, enabled: bool) -> ControlState:
        with self.lock:
            return self._control_changed(self.c.risk.set_confirmation_mode(enabled))

    def emergency_stop(self, reason: str = "", flatten: bool = False) -> ControlState:
        with self.lock:
            ctrl = self.c.risk.emergency_stop(reason)
            for did in list(self.pending):
                self.reject(did, "emergency stop")
            if flatten:
                for sym in sorted({l.position.symbol for l in self.book.lots}):
                    self.close_symbol(sym, ExitReason.EMERGENCY_STOP)
            return self._control_changed(ctrl)

    def _control_changed(self, ctrl: ControlState) -> ControlState:
        self.bus.publish(EventType.CONTROL, ctrl)
        self._publish_alerts(self.alerts.on_control(ctrl))
        self.bus.publish(EventType.STATUS, self.status())
        return ctrl

    # ------------------------------------------------------------------ output
    def _finalize(self, d: Decision) -> Decision:
        try:
            d = d.model_copy(update={"explanation": self.c.explainer.explain_decision(d)})
        except Exception:
            pass
        # replace an earlier version of the same decision (confirmation flow) or append
        for i, old in enumerate(self.decisions):
            if old.id == d.id:
                self.decisions[i] = d
                break
        else:
            self.decisions.append(d)
        self.bus.publish(EventType.DECISION, d)
        self._publish_alerts(self.alerts.on_decision(d))
        return d

    def status(self) -> StatusSnapshot:
        with self.lock:
            recent = list(self.decisions)[-20:][::-1]
            return StatusSnapshot(
                control=self.control(), portfolio=self.book.portfolio(), agent_mode=self.agent_mode,
                active_symbol=self.symbol, current_regime=self.current_regime, active_strategy=self.active_strategy,
                last_decision=recent[0] if recent else None, recent_decisions=recent, timestamp=self._now(),
            )
