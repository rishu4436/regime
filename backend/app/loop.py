"""The trading loop: control -> (protective exits) -> regime -> strategy -> risk -> execute/confirm -> explain -> log.

Built only against the contract Protocols (engine / risk / exchange / explainer). One active agent at a time:
either a ManualStrategy or an AutoConfig. `tick()` runs exactly one cycle on the next bar; the API runs it on a
timer (`/api/loop/start`) or on demand (`/api/loop/tick`, used by the e2e tests).
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from backend.contracts import (
    Action, ActivationMode, AgentMode, AutoConfig, Candle, ControlState, Decision, EventType, ExecutionPath, ExecutionResult,
    ExitReason, ExitRules, ManualStrategy, OrderRequest, OrderSide, OrderStatus, Regime, RiskVerdict, Signal,
    Quote, RiskCheck, StatusSnapshot, Timeframe, TradingMode, utc_now,
)
from backend.contracts import AlertLevel
from backend.monitoring import AlertManager, EventLog, PerformanceTracker, one_prefix

from .events import EventBus
from .exits import check_protective_exit, exit_reason_of, trailing_level
from .feed import PollingFeed, ReplayFeed
from .fallbacks import DAILY_LOSS_LIMIT_PCT
from . import live as livemod
from .portfolio import Lot, PortfolioBook
from .settings import Settings
from .state import AgentStateStore, state_path
from .wiring import Components, call_with_supported


class AgentStateError(RuntimeError):
    """Invalid state for the request (mapped to HTTP 409)."""


def call_check_exits(risk: Any, portfolio, prices: dict[str, float], bars: dict[str, Candle]) -> list[Signal]:
    """The loop's exit path: RiskManager.check_exits with the latest bar OHLC (contract v0.2 `bars`)."""
    return list(risk.check_exits(portfolio, prices, bars=bars))


def _quote_error(e: Exception) -> str:
    """Exchange quote errors as plain decision text (Bot 4: market closed / no liquidity / below minimum ...)."""
    msg = str(e) or type(e).__name__
    code = getattr(e, "code", None)
    return f"{msg} [{code}]" if code and str(code) not in msg else msg


def _blocked(reasons: list[str]) -> str:
    """Risk reasons with exactly one 'Blocked' prefix each (Bot 3 already writes 'Blocked (<check>): ...')."""
    return "; ".join(one_prefix("Blocked", r) for r in reasons) or "Blocked"


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
        self.sim = settings.trading_mode == TradingMode.SIM
        self.live = settings.trading_mode == TradingMode.LIVE
        self.allow_live = livemod.apply_allow_live(comps.risk, settings)  # RISK_ALLOW_LIVE -> RiskConfig.allow_live (LIVE only)
        self.book = PortfolioBook(self._live_starting_capital(settings, comps), settings.trading_mode)
        self.state = AgentStateStore(state_path(settings.state_dir, settings.trading_mode))
        self.exec_meta: "OrderedDict[str, dict]" = OrderedDict()  # order_id -> {quote_expires_at}
        self.started_at = utc_now()
        self.last_tick_at: Optional[datetime] = None
        self.last_error: Optional[str] = None
        self.restarts = 0
        self.bus.enrich = self.enrich_payload
        self.wallet_settings = livemod.WalletSettingsProvider(
            comps.exchange, settings.trading_mode, on_error=lambda m: self._alert(AlertLevel.WARNING, "WALLET_SETTINGS", m))
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
            self._alert(AlertLevel.CRITICAL, "LIVE_MODE", "LIVE trading enabled: real orders are placed autonomously "
                        "(confirmation only if confirmation mode is on); risk LIVE caps apply")
            if not self.allow_live:
                self._alert(AlertLevel.WARNING, "LIVE_MODE", "LIVE risk switch is off (allow_live=False, set RISK_ALLOW_LIVE=true): "
                            "every BUY will be blocked")
        self._restore()

    @staticmethod
    def _live_starting_capital(settings: Settings, comps: Components) -> float:
        """LIVE: start the book from the wallet's USDT balance when the exchange exposes one (never more than the
        configured STARTING_CAPITAL, so the risk % sizing stays tiny); otherwise STARTING_CAPITAL."""
        if settings.trading_mode != TradingMode.LIVE:
            return settings.starting_capital
        try:
            _, bal, _ = livemod.read_exchange_wallet(comps.exchange)
        except Exception:
            bal = None
        usdt = (bal or {}).get(livemod.QUOTE_ASSET, 0.0)
        return min(settings.starting_capital, usdt) if usdt > 0 else settings.starting_capital

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
            hook = getattr(self.c.risk, "apply_manual_strategy", None)  # optional (Bot 3): per-symbol risk/exit rules
            if callable(hook):
                hook(strategy)
            self._on_activate(strategy.symbol, strategy.timeframe, strategy.activation)
            self.state.update(agent={"kind": "manual", "strategy": strategy.model_dump(mode="json")})
            return self.status()

    def activate_auto(self, config: AutoConfig) -> StatusSnapshot:
        with self.lock:
            self._check_switch(config.symbol)
            self.manual, self.auto = None, config
            self.active_strategy = None
            self._on_activate(config.symbol, config.timeframe, config.activation)
            self.state.update(agent={"kind": "auto", "config": config.model_dump(mode="json")})
            return self.status()

    def _on_activate(self, symbol: str, tf: Timeframe, activation: ActivationMode) -> None:
        self._reject_all_pending("agent switched")
        self.finished = False
        self.current_regime = None
        ctrl = self.c.risk.set_confirmation_mode(activation == ActivationMode.CONFIRMATION)
        self.bus.publish(EventType.CONTROL, ctrl)
        self.bus.publish(EventType.STATUS, self.status())

    def deactivate(self) -> StatusSnapshot:
        with self.lock:
            self._reject_all_pending("agent deactivated")
            self.manual = self.auto = None
            self.state.update(agent=None)
            self.active_strategy = None
            st = self.status()
            self.bus.publish(EventType.STATUS, st)
            return st

    # ------------------------------------------------------------------ the cycle
    def tick(self) -> list[Decision]:
        with self.lock:
            sym = self.symbol
            if sym is None:
                raise AgentStateError("no active agent: activate a manual strategy or auto config first")
            self.last_tick_at = utc_now()  # heartbeat: every cycle counts, even without a new bar
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
                    reason=one_prefix("Emergency stop", control.reason, "active") + "; staying out")))
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
            verdict = self._risk_check(signal, pf, control)  # LIVE BUY: pass 1 with LiveContext (no quote yet)
            if not verdict.approved:
                out.append(self._finalize(Decision(action=Action.STAY_OUT, risk_verdict=verdict,
                                                   reason=f"{signal.action.value} not executed: " + _blocked(verdict.reasons), **base)))
            elif signal.action == Action.SELL:
                lots = self.book.lots_for(sym)
                out.append(self._exit(lots, signal, exit_reason_of(signal), regime, verdict))
            elif verdict.requires_confirmation:  # only when confirmation mode / CONFIRMATION activation is on (LIVE too)
                d = self._pending_decision(signal, verdict, base)
                out.append(d)
            else:
                out.append(self._buy(signal, verdict, base))
            return self._end_tick(out)

    def _end_tick(self, out: list[Decision]) -> list[Decision]:
        self._persist()
        if self.live:  # surface Bot 3's limit warnings (e.g. quota can't cover selling all positions) as alerts
            try:
                ctx = livemod.make_live_context(wallet_limits=self.wallet_settings.get(), daily_live_notional_usdt=self.daily_live_notional())
                self._alert_new_warnings(list(livemod.risk_live_limits(self.c.risk, self.book.portfolio(), ctx).get("warnings") or []))
            except Exception:
                pass
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
        if sig.position_id:
            return next((l for l in lots if l.position.id == sig.position_id), None)
        if len(lots) <= 1:
            return lots[0] if lots else None
        want = exit_reason_of(sig)
        for l in lots:
            p = l.position
            hit = check_protective_exit(open_=bar.open, high=bar.high, low=bar.low, stop_loss_price=p.stop_loss_price,
                                        take_profit_price=p.take_profit_price,
                                        trail_level=trailing_level(p.highest_price, p.trailing_stop_pct, p.avg_entry_price))
            if hit and (want is None or hit.reason == want) and (sig.price is None or abs(hit.price - sig.price) < 1e-9 * max(1, hit.price)):
                return l
        return lots[0]

    # ------------------------------------------------------------------ execution
    # ------------------------------------------------------------------ LIVE (Bot 3 two-pass check, duck-typed)
    def _live_context(self, sym: str, quote: Optional[Quote] = None) -> tuple[Any, Optional[dict[str, float]], Optional[float], Optional[datetime], list[RiskCheck]]:
        """Fresh facts for risk: now, data_timestamp (= price FETCH time, else bar CLOSE), bar, reference price,
        balances {USDT, BNB, ...}, today's LIVE notional, plus Bot 4's per-token facts when the client has them."""
        ex = self.c.exchange
        errs: list[RiskCheck] = []
        price = data_ts = balances = None
        try:
            price, data_ts = float(ex.get_price(sym)), utc_now()
        except Exception as e:
            errs.append(RiskCheck(name="live_price", passed=False, detail=f"latest price unavailable: {type(e).__name__}: {e}"))
            if self._bar is not None:
                data_ts = self._bar.timestamp + livemod.bar_interval(self.timeframe)  # bar CLOSE, never the open
        try:
            _, balances, _ = livemod.read_exchange_wallet(ex)
        except Exception as e:
            errs.append(RiskCheck(name="live_wallet", passed=False, detail=f"wallet unavailable: {type(e).__name__}: {e}"))
        if balances is None:
            errs.append(RiskCheck(name="live_wallet_balances", passed=False,
                                  detail="wallet balances unavailable (exchange client has no get_wallet/get_balances): no BUYs"))
        extra: dict = {"wallet_limits": self.wallet_settings.get()}
        fn = getattr(ex, "get_token_status", None)  # Bot 4: trading status, min order, reference-price-only
        if callable(fn):
            try:
                extra["token_status"] = livemod.to_token_status(fn(sym))
            except Exception as e:
                errs.append(RiskCheck(name="live_token_status", passed=False, detail=f"token status unavailable: {type(e).__name__}: {e}"))
        ref = None
        fn = getattr(ex, "get_reference_price", None)
        if callable(fn):
            try:
                ref = float(fn(sym))
            except Exception:
                ref = None
        if quote is not None and livemod.quote_price_impact(quote) is not None:
            extra["price_impact_pct"] = livemod.quote_price_impact(quote)
        ref = ref or price
        ctx = livemod.make_live_context(now=utc_now(), data_timestamp=data_ts, bar=self._bar, bar_interval=livemod.bar_interval(self.timeframe),
                                        reference_price=ref, quote=quote, balances=balances,
                                        daily_live_notional_usdt=self.daily_live_notional(), **extra)
        return ctx, balances, ref, data_ts, errs

    def _risk_check(self, signal: Signal, pf, control: ControlState, quote: Optional[Quote] = None) -> RiskVerdict:
        """risk.check; for a LIVE BUY with a LIVE-aware risk (Bot 3), with context=LiveContext. App-side guards
        (wallet balances present, token family) are folded into the verdict as failed checks."""
        if not (self.live and signal.action == Action.BUY):
            return self.c.risk.check(signal, pf, control)
        ctx, balances, ref, data_ts, errs = self._live_context(signal.symbol, quote)
        if livemod.risk_supports_context(self.c.risk):
            v = self.c.risk.check(signal, pf, control, context=ctx)
        else:  # older risk: the app's own LIVE caps (same RISK_LIVE_* limits) stand in
            v = self.c.risk.check(signal, pf, control)
            notional = (quote.notional if quote else None) or v.adjusted_notional or signal.proposed_notional
            if v.approved and notional > self.s.live_max_order_usdt:
                v = v.model_copy(update={"adjusted_notional": self.s.live_max_order_usdt})
                notional = self.s.live_max_order_usdt
            errs += [c for c in livemod.app_live_checks(self.s, signal, pf, notional, balances, quote, ref, data_ts,
                                                        self.daily_live_notional()) if v.approved or not c.passed]
        errs += self._token_family_checks(signal.symbol)
        failed = [c for c in errs if not c.passed]
        checks = list(v.checks) + errs
        if failed and v.approved:
            return v.model_copy(update={"approved": False, "adjusted_notional": 0.0, "checks": checks,
                                        "reasons": [f"Blocked ({c.name}): {c.detail}" for c in failed]})
        return v.model_copy(update={"checks": checks})

    def _token_family_checks(self, sym: str) -> list[RiskCheck]:
        if self.s.live_allowed_tokens:
            ok = sym in self.s.live_allowed_tokens
            return [RiskCheck(name="live_allowed_tokens", passed=ok, detail="" if ok else f"{sym} is not in LIVE_ALLOWED_TOKENS")]
        if sym.endswith("x"):  # xStock: RFQ + wallet-gated, thin liquidity -> excluded by default
            return [RiskCheck(name="live_token_family", passed=False, detail=f"{sym} is an xStock token, excluded in LIVE by default "
                              "(set LIVE_ALLOWED_TOKENS to include it)")]
        return []

    def daily_live_notional(self) -> float:
        return float((self.state.get("live_bought") or {}).get(utc_now().date().isoformat(), 0.0))

    def _record_live_buy(self, res: ExecutionResult) -> None:
        day = utc_now().date().isoformat()
        self.state.update(live_bought={day: self.daily_live_notional() + res.notional})

    def wallet(self) -> dict:
        """GET /api/wallet (Bot 1 shape). LIVE/PAPER: from the exchange (get_wallet/get_balances); SIM: simulated from
        the book. Never shows made-up balances in LIVE. limits = Bot 3's live_limits() merged over the app's."""
        with self.lock:
            pf = self.book.portfolio()
            address, bal, _src = None, None, "none"
            if not self.sim:
                try:
                    address, bal, _src = livemod.read_exchange_wallet(self.c.exchange)
                except Exception:
                    bal = None
            address = address or self.s.wallet_address
            connected = bal is not None
            if bal is None and not self.live:
                bal = {livemod.GAS_ASSET: self.s.sim_bnb_balance, livemod.QUOTE_ASSET: round(pf.cash, 8)}
                for p in pf.positions:
                    bal[p.symbol] = bal.get(p.symbol, 0.0) + p.quantity
                connected = True
                address = address or ("0xSIMULATED-WALLET" if self.sim else None)
            bal = bal or {}
            prices = {p.symbol: (p.current_price or p.avg_entry_price) for p in pf.positions}
            rows = []
            for asset, amt in sorted(bal.items(), key=lambda kv: (kv[0] not in ("BNB", "USDT"), kv[0])):
                usd = amt if asset == livemod.QUOTE_ASSET else (amt * prices[asset] if asset in prices else None)
                rows.append({"symbol": asset, "amount": round(amt, 10), "usd_value": None if usd is None else round(usd, 8)})
            wl = self.wallet_settings.get() if self.live else None
            ctx = livemod.make_live_context(wallet_limits=wl, balances=bal if connected else None,
                                            daily_live_notional_usdt=self.daily_live_notional())
            rl = livemod.risk_live_limits(self.c.risk, pf, ctx)
            used = rl.get("daily_used_usdt")
            if used is None:
                used = (rl.get("risk_limits") or {}).get("daily_used_usdt")
            used = max(used or 0.0, self.daily_live_notional())
            daily = rl.get("daily_limit_usdt") or rl.get("daily_notional_cap_usdt") or self.s.live_max_daily_notional_usdt
            limits = {**rl,  # Bot 3's live_limits() keys as-is (limit_source, wallet_limits, risk_limits, ...)
                      "per_order_cap_usdt": rl.get("per_order_cap_usdt") or self.s.live_max_order_usdt,
                      "daily_limit_usdt": daily, "daily_used_usdt": used,
                      "daily_remaining_usdt": rl.get("daily_remaining_usdt") if rl.get("daily_remaining_usdt") is not None
                      else max(0.0, daily - used),
                      "allowed_tokens": rl.get("allowed_tokens") or self.allowed_tokens(),
                      "limit_source": rl.get("limit_source") or "app",
                      "warnings": list(rl.get("warnings") or [])}
            self._alert_new_warnings(limits["warnings"])
            limits.setdefault("daily_loss_cap_usdt", self.s.live_max_daily_loss_usdt)
            limits.setdefault("max_exposure_usdt", self.s.live_max_exposure_usdt)
            return {
                "provider": livemod.wallet_provider(self.c.exchange, self.s.trading_mode), "address": address, "chain": "BSC",
                "chain_id": livemod.BSC_CHAIN_ID, "connected": connected, "balances": rows,
                "bnb_gas_reserve": rl.get("bnb_gas_reserve", self.s.live_min_bnb), "limits": limits, "updated_at": utc_now(),
            }

    def _alert_new_warnings(self, warnings: list) -> None:
        """Bot 3 live_limits warnings [{code, message}] -> one ALERT per newly appearing code."""
        seen = getattr(self, "_warning_codes", set())
        now = set()
        for w in warnings:
            code = w.get("code") if isinstance(w, dict) else getattr(w, "code", None)
            msg = w.get("message") if isinstance(w, dict) else getattr(w, "message", "")
            if not code:
                continue
            now.add(code)
            if code not in seen:
                self._alert(AlertLevel.WARNING, f"LIVE_LIMIT_{code}".upper(), msg or code)
        self._warning_codes = now

    def allowed_tokens(self) -> list[str]:
        if self.s.live_allowed_tokens:
            return list(self.s.live_allowed_tokens)
        try:
            syms = [t.symbol for t in self.c.exchange.search_tokens("")]
        except Exception:
            syms = []
        return [s for s in syms if not (self.live and s.endswith("x"))]

    def live_status(self) -> dict:
        """GET /api/live/status and /api/system.live (Bot 1 shape)."""
        allow = bool(self.allow_live if self.allow_live is not None else livemod.risk_allow_live(self.c.risk))
        allow = bool(livemod.risk_allow_live(self.c.risk)) if livemod.risk_allow_live(self.c.risk) is not None else allow
        connected = False
        if self.live:
            try:
                connected = livemod.read_exchange_wallet(self.c.exchange)[1] is not None
            except Exception:
                connected = False
        reasons: list[str] = []
        if not self.live:
            reasons.append(f"TRADING_MODE is {self.s.trading_mode.value}, not LIVE")
        if not self.s.live_trading_enabled:
            reasons.append("LIVE_TRADING_ENABLED is not true")
        if not self.s.binance_live_enabled:
            reasons.append("BINANCE_LIVE_ENABLED is not true")
        if not allow:
            reasons.append("risk allow_live is off (RISK_ALLOW_LIVE=true)")
        if self.live and not connected:
            reasons.append("wallet not connected (no balances from the exchange client)")
        ctrl = self.control()
        if ctrl.emergency_stopped:
            reasons.append(one_prefix("Emergency stop", ctrl.reason, "active"))
        if ctrl.paused:
            reasons.append(one_prefix("Paused", ctrl.reason, "by user"))
        return {"live_trading_enabled": self.s.live_trading_enabled, "binance_live_enabled": self.s.binance_live_enabled,
                "risk_allow_live": allow, "wallet_connected": connected, "can_trade": not reasons, "blocked_reasons": reasons}

    def enrich_payload(self, payload: Any) -> Any:
        return livemod.enrich_payload(payload, self.exec_meta.get)

    # ------------------------------------------------------------------ persistence (24/7)
    def _persist(self) -> None:
        ctrl = self.control()
        data: dict = {"control": {"emergency_stopped": ctrl.emergency_stopped, "paused": ctrl.paused, "reason": ctrl.reason,
                                  "confirmation_mode": ctrl.confirmation_mode}}
        if not self.sim:
            data["book"] = self.book.snapshot()
        self.state.update(**data)

    def _restore(self) -> None:
        """Resume after a restart: active agent, open lots (PAPER/LIVE), control state. An emergency stop survives."""
        st = self.state
        if st.get("_corrupt"):
            self._alert(AlertLevel.WARNING, "STATE_RESTORE", f"agent state file {st.path} unreadable; starting clean")
        book = st.get("book")
        if book and not self.sim:
            try:
                self.book.restore(book)
            except Exception as e:
                self._alert(AlertLevel.WARNING, "STATE_RESTORE", f"could not restore open positions: {e}")
        agent = st.get("agent")
        try:
            if agent and agent.get("kind") == "manual":
                self.activate_manual(ManualStrategy.model_validate(agent["strategy"]))
            elif agent and agent.get("kind") == "auto":
                self.activate_auto(AutoConfig.model_validate(agent["config"]))
        except Exception as e:
            self._alert(AlertLevel.WARNING, "STATE_RESTORE", f"could not resume the saved agent: {e}")
        ctrl = st.get("control") or {}
        if "confirmation_mode" in ctrl:
            self.c.risk.set_confirmation_mode(bool(ctrl["confirmation_mode"]))
        if ctrl.get("paused"):
            self.c.risk.pause(ctrl.get("reason") or "")
        if ctrl.get("emergency_stopped"):
            self.c.risk.emergency_stop(ctrl.get("reason") or "restored after restart")
            self._alert(AlertLevel.CRITICAL, "EMERGENCY_STOP", one_prefix("Emergency stop", ctrl.get("reason"), "active")
                        + " (restored after restart; reset to trade)")
        if agent or book or ctrl:
            self._alert(AlertLevel.INFO, "STATE_RESTORED", f"resumed {(agent or {}).get('kind') or 'no'} agent, "
                        f"{len(self.book.lots)} open lot(s)" + (", emergency stop active" if ctrl.get("emergency_stopped") else ""))

    def heartbeat(self) -> dict:
        now = utc_now()
        return {"started_at": self.started_at, "uptime_s": round((now - self.started_at).total_seconds(), 1),
                "last_tick_at": self.last_tick_at, "seconds_since_tick": None if self.last_tick_at is None
                else round((now - self.last_tick_at).total_seconds(), 1), "restarts": self.restarts, "last_error": self.last_error}

    def _execute(self, side: OrderSide, sym: str, notional: float, decision_id: str, quantity: Optional[float] = None,
                 fill_price: Optional[float] = None, quote: Optional[Quote] = None) -> ExecutionResult:
        ex = self.c.exchange
        mode = self.s.trading_mode
        if ex.mode != mode:  # hard SIM/PAPER/LIVE separation
            res = ExecutionResult(client_order_id=decision_id, symbol=sym, side=side, status=OrderStatus.REJECTED,
                                  path=OrderRequest.model_fields["path"].default, mode=mode, error="exchange mode mismatch")
        else:
            try:
                if fill_price:
                    self._set_sim_price(sym, fill_price)  # SIM: protective exits fill at the stop / target / gap open
                quote = quote or ex.get_quote(sym, side, notional)  # LIVE BUY: the quote the pre-trade checks approved
                order = OrderRequest(symbol=sym, side=side, notional=quote.notional, quantity=quantity, quote_id=quote.quote_id,
                                     path=quote.path, mode=mode, decision_id=decision_id)
                # dispatch on Quote.path: the exchange rejects an order sent to the wrong path (Bot 4)
                res = ex.execute_rfq(order, quote) if quote.path == ExecutionPath.RFQ else ex.execute_swap(order, quote)
            except Exception as e:  # never let an exchange error kill the loop
                res = ExecutionResult(client_order_id=decision_id, symbol=sym, side=side, status=OrderStatus.FAILED,
                                      path=OrderRequest.model_fields["path"].default, mode=mode, error=f"{type(e).__name__}: {e}")
            finally:
                if fill_price and self._bar is not None:
                    self._set_sim_price(sym, self._bar.close)
        if quote is not None and res.order_id:
            self.exec_meta[res.order_id] = {"quote_expires_at": quote.expires_at.isoformat()}
            while len(self.exec_meta) > 5000:
                self.exec_meta.popitem(last=False)
        livemod.record_execution(self.c.risk, res)  # Bot 3: LIVE BUY fills count towards the daily notional cap
        if self.live and side == OrderSide.BUY and res.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
            self._record_live_buy(res)
        self.bus.publish(EventType.EXECUTION, res, decision_id=decision_id)
        self._publish_alerts(self.alerts.on_execution(res))
        return res

    def _buy(self, signal: Signal, verdict: RiskVerdict, base: dict, decision_id: Optional[str] = None) -> Decision:
        did = decision_id or Decision.model_fields["id"].default_factory()
        notional = verdict.adjusted_notional or signal.proposed_notional
        if notional <= 0:
            return self._finalize(Decision(id=did, action=Action.STAY_OUT, risk_verdict=verdict, reason="BUY approved with zero size", **base))
        quote = None
        if self.live:  # Bot 3 pass 2: quote for the approved notional, re-check with the quote and a fresh `now`
            try:
                quote = self.c.exchange.get_quote(signal.symbol, OrderSide.BUY, notional)
            except Exception as e:  # market closed / no liquidity / below minimum: a normal decision, not a crash
                return self._finalize(Decision(id=did, action=Action.STAY_OUT, risk_verdict=verdict,
                                               reason=f"BUY not executed: no quote ({_quote_error(e)})", **base))
            v2 = self._risk_check(signal, self.book.portfolio(), self.control(), quote=quote)
            if not v2.approved:
                return self._finalize(Decision(id=did, action=Action.STAY_OUT, risk_verdict=v2,
                                               reason="BUY not executed: " + _blocked(v2.reasons), **base))
            verdict = v2
        res = self._execute(OrderSide.BUY, signal.symbol, notional, did, quote=quote)
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
            return self._finalize(Decision(id=did, action=Action.SELL, execution=last,
                                           reason=one_prefix(tag, signal.reason, keep_sentence=False), **base))
        return self._finalize(Decision(id=did, action=Action.STAY_OUT, execution=last,
                                       reason=f"{tag} exit not filled ({last.status.value if last else '-'}), retrying next bar", **base))

    def _pending_decision(self, signal: Signal, verdict: RiskVerdict, base: dict) -> Decision:
        did = Decision.model_fields["id"].default_factory()
        notional = verdict.adjusted_notional or signal.proposed_notional
        note = ""
        ex = ExecutionResult(client_order_id=did, symbol=signal.symbol, side=OrderSide.BUY, status=OrderStatus.PENDING_CONFIRMATION,
                             path=OrderRequest.model_fields["path"].default, mode=self.s.trading_mode,
                             notional=notional, timestamp=self._now())
        d = self._finalize(Decision(id=did, action=Action.BUY, risk_verdict=verdict, execution=ex,
                                    reason=f"awaiting user confirmation{note}: {signal.reason}", **base))
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
            verdict = self._risk_check(p.signal, self.book.portfolio(), control)  # re-check at approval time
            if not verdict.approved:
                return self._finalize(Decision(id=decision_id, action=Action.STAY_OUT, risk_verdict=verdict,
                                               reason="approved by user but not executed: " + _blocked(verdict.reasons), **base))
            d = self._buy(p.signal, verdict, base, decision_id=decision_id)
            self.bus.publish(EventType.STATUS, self.status())
            return d

    def reject(self, decision_id: str, reason: str = "", by_user: bool = True) -> Decision:
        with self.lock:
            p = self.pending.pop(decision_id, None)
            if p is None:
                raise KeyError(decision_id)
            d0 = p.decision
            who = "Rejected by user" if by_user else "Not executed"
            text = one_prefix(who, reason) if reason else who
            ex = d0.execution.model_copy(update={"status": OrderStatus.REJECTED, "error": text})
            return self._finalize(d0.model_copy(update={"action": Action.STAY_OUT, "execution": ex, "explanation": None,
                                                        "reason": text}))

    def _reject_all_pending(self, reason: str) -> None:
        """A previous agent's confirmations must never execute under a new agent (or after deactivation)."""
        for did in list(self.pending):
            self.reject(did, reason, by_user=False)

    def close_position(self, position_id: str, reason: ExitReason = ExitReason.MANUAL) -> Decision:
        """Close one lot (Position.id) at the last price."""
        with self.lock:
            lot = next((l for l in self.book.lots if l.position.id == position_id), None)
            if lot is None:
                raise KeyError(position_id)
            return self._close_lots([lot], lot.position.symbol, reason)

    def close_symbol(self, symbol: str, reason: ExitReason = ExitReason.MANUAL) -> Decision:
        """Close every lot of `symbol` at the last price (manual override / emergency flatten)."""
        with self.lock:
            lots = self.book.lots_for(symbol)
            if not lots:
                raise KeyError(symbol)
            return self._close_lots(lots, symbol, reason)

    def _close_lots(self, lots: list[Lot], symbol: str, reason: ExitReason) -> Decision:
        with self.lock:
            px = self.book.prices.get(symbol) or self.c.exchange.get_price(symbol)
            sig = Signal(symbol=symbol, action=Action.SELL, source=self.agent_mode or AgentMode.MANUAL,
                         strategy=lots[0].position.strategy, reason="close requested by user" if reason == ExitReason.MANUAL else "close requested (emergency flatten)", price=px,
                         exit_reason=reason, position_id=lots[0].position.id if len(lots) == 1 else None,
                         timestamp=self._now())
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

    def reset_emergency_stop(self) -> ControlState:
        with self.lock:
            return self._control_changed(self.c.risk.reset_emergency_stop())

    def set_confirmation_mode(self, enabled: bool) -> ControlState:
        with self.lock:
            return self._control_changed(self.c.risk.set_confirmation_mode(enabled))

    def emergency_stop(self, reason: str = "", flatten: bool = False) -> ControlState:
        with self.lock:
            ctrl = self.c.risk.emergency_stop(reason)
            self._reject_all_pending("emergency stop")
            if flatten:
                for sym in sorted({l.position.symbol for l in self.book.lots}):
                    self.close_symbol(sym, ExitReason.EMERGENCY_STOP)
            return self._control_changed(ctrl)

    def _control_changed(self, ctrl: ControlState) -> ControlState:
        self._persist()
        self.bus.publish(EventType.CONTROL, ctrl)
        self._publish_alerts(self.alerts.on_control(ctrl))
        self.bus.publish(EventType.STATUS, self.status())
        return ctrl

    # ------------------------------------------------------------------ market view (no future bars in SIM)
    def sim_now(self, symbol: str, timeframe: Timeframe) -> Optional[datetime]:
        """SIM: the time of the bar the loop is on (before the first tick: the end of the warm-up). None otherwise."""
        if not self.sim:
            return None
        with self.lock:
            if self._bar is not None:
                return self._bar.timestamp
            if self.symbol is not None:
                return self.feed.start_time(self.symbol, self.timeframe)
            return self.feed.start_time(symbol, timeframe)

    def view_candles(self, symbol: str, timeframe: Timeframe, limit: int) -> list[Candle]:
        """Candles for the UI / regime preview. In SIM never past the loop's current bar: the client trims itself
        when it supports set_replay_index; otherwise (or before the first tick) trim the replay series here."""
        if not self.sim:
            return self.c.exchange.get_candles(symbol, timeframe, limit)
        with self.lock:
            now = self.sim_now(symbol, timeframe)
            series = self.feed.series(symbol, timeframe)
            out = [c for c in series if now is None or c.timestamp <= now]
            return out[-limit:] if limit > 0 else []

    def history_candles(self, symbol: str, timeframe: Timeframe, limit: Optional[int] = None) -> list[Candle]:
        """Full history, independent of the replay position: SIM -> the feed's cached seeded series (last `limit`),
        PAPER/LIVE -> the exchange's candles (`limit` or 500)."""
        if self.sim:
            return self.feed.history(symbol, timeframe, limit)
        return self.c.exchange.get_candles(symbol, timeframe, limit or 500)

    def view_price(self, symbol: str) -> float:
        if not self.sim:
            return self.c.exchange.get_price(symbol)
        with self.lock:
            c = self.view_candles(symbol, self.timeframe, 1)
            return c[-1].close if c else self.c.exchange.get_price(symbol)

    # ------------------------------------------------------------------ LLM (read-only)
    def llm_context(self) -> tuple[StatusSnapshot, dict]:
        with self.lock:
            return self.status(), {"performance": self.perf.summary(self.book.portfolio()), "alerts": self.alerts.alerts(20)}

    def summarize(self) -> str:
        status, kw = self.llm_context()
        return call_with_supported(self.c.explainer.summarize_state, status, **kw)

    def answer(self, question: str) -> str:
        status, kw = self.llm_context()
        return call_with_supported(self.c.explainer.answer, question, status, **kw)

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
