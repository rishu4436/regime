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
from datetime import datetime, timedelta
from typing import Any, Optional

from backend.contracts import (
    Action, ActivationMode, AgentMode, AutoConfig, Candle, ControlState, Decision, EventType, ExecutionPath, ExecutionResult,
    ExitReason, ExitRules, ManualStrategy, OrderRequest, OrderSide, OrderStatus, Regime, RiskVerdict, Signal,
    Portfolio, Position, Quote, RiskCheck, StatusSnapshot, Timeframe, TradingMode, utc_now,
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
from .wiring import Components, call_with_supported, supported_kwargs

PENDING_EXIT = "pending_exit"        # exit held while the BUY settles on chain (Bot 1 / Bot 5 code)
ORDER_IN_FLIGHT = "order_in_flight"  # Bot 4 executor single-flight refusal (verbatim)


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
        self.cooldowns: dict[str, dict] = {}       # symbol -> {until, code, reason}: exchange off-hours errors (20 min)
        self.chain_pending: dict[str, dict] = {}   # client_order_id -> in-flight BUY (synthetic position)
        self._settled: set[str] = set()
        self._token_status_cache: dict[str, tuple[float, Optional[dict]]] = {}
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
            self._sync_explainer_limits()
            now = self._now()
            self._set_sim_price(sym, bar.close)
            self.book.on_bar(sym, bar, now)
            self.bus.publish(EventType.PRICE, {"symbol": sym, "price": bar.close, "candle": bar.model_dump(mode="json")})
            out: list[Decision] = []

            # 0) in-flight LIVE buys: reconcile first, so a fill confirmed now is a real lot for the exits below
            if not self.sim:
                out.extend(self._poll_chain_pending())
            # 1) protective exits (SL / TP / trailing) - always, even when paused; never need confirmation
            regime = self._detect(window)
            sigs = call_check_exits(self.c.risk, self._pf(), {sym: bar.close}, {sym: bar})
            used: set[int] = set()
            for sig in sigs:
                if sig.action == Action.SELL:
                    if self._is_pending_target(sig):  # never sell a position that is still settling on chain
                        out.append(self._hold_exit(sig, "risk.check_exits", regime))
                        continue
                    lot = self._match_lot(sig, bar, used)
                    if lot is not None:
                        used.add(id(lot))
                        out.append(self._exit([lot], sig, exit_reason_of(sig), regime, verdict=None))
            self.book.raise_highs(sym, bar.high)

            # 2) control
            control = self.control()
            if control.emergency_stopped:
                for p in self._pending_for(None):
                    if not p.get("held_exit"):
                        out.append(self._hold_exit(None, "emergency_stop", regime, entry=p))
                out.append(self._finalize(Decision(
                    mode=self.s.trading_mode, agent_mode=self.agent_mode, symbol=sym, regime=regime, timestamp=now,
                    strategy=self.active_strategy or "NONE", action=Action.STAY_OUT,
                    reason=one_prefix("Emergency stop", control.reason, "active") + "; staying out")))
                return self._end_tick(out)

            # 3) strategy (engine) -> 4) risk -> 5) execute / confirm
            pf = self._pf()  # + in-flight buys as synthetic positions (engine and risk both see them)
            if not self.sim:
                self._update_market_context(sym, pf)
            if self.manual:
                signal = self.c.engine.evaluate_manual(self.manual, window, pf)
            else:
                signal = self.c.engine.evaluate_auto(self.auto, window, pf)
                self.active_strategy = signal.strategy if signal.preset else "NONE"
                if signal.regime is not None:
                    regime = self.current_regime = signal.regime
            base = dict(mode=self.s.trading_mode, agent_mode=self.agent_mode, symbol=sym, regime=regime, timestamp=now,
                        strategy=signal.strategy, signal=signal)
            if signal.action == Action.STAY_OUT:  # incl. Bot 2's live_* / insufficient_data: a logged no-trade
                out.append(self._finalize(Decision(action=Action.STAY_OUT, reason=signal.reason, **base)))
                return self._end_tick(out)
            cd = self._cooldown(sym) if signal.action == Action.BUY else None
            if cd:
                out.append(self._finalize(Decision(action=Action.STAY_OUT, reason=f"BUY not executed: {cd}", **base)))
                return self._end_tick(out)
            waiting = self._pending_buy_for(sym) if signal.action == Action.BUY else None
            if waiting is not None:  # one confirmation per symbol/agent at a time: no stale approval queue
                out.append(self._finalize(Decision(action=Action.STAY_OUT, reason=(
                    f"BUY not executed: awaiting your approval of the pending BUY ({waiting.decision.id}); "
                    f"no new order until you approve or reject it"), **base)))
                return self._end_tick(out)
            verdict = self._risk_check(signal, pf, control)  # LIVE BUY: pass 1 with LiveContext (no quote yet)
            if not verdict.approved:
                out.append(self._finalize(Decision(action=Action.STAY_OUT, risk_verdict=verdict,
                                                   reason=f"{signal.action.value} not executed: " + _blocked(verdict.reasons), **base)))
            elif signal.action == Action.SELL:
                lots = self.book.lots_for(sym)
                if signal.position_id:  # the engine targeted one lot: sell only that one (others exit on their own signal)
                    lots = [l for l in lots if l.position.id == signal.position_id] or lots
                if self._pending_for(sym):
                    out.append(self._hold_exit(signal, "engine", regime))
                if not lots and not self._pending_for(sym):
                    out.append(self._exit([], signal, exit_reason_of(signal), regime, verdict))
                for lot in lots:  # one Decision + execution per lot, so the log replays to the real book
                    out.append(self._exit([lot], signal, exit_reason_of(signal), regime, verdict))
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

    # ------------------------------------------------------------------ LIVE: cooldowns, chain, session, engine context
    COOLDOWN = timedelta(minutes=20)

    def _maybe_cooldown(self, sym: str, err: Any) -> str:
        code = livemod.off_hours_code(err)
        if not code:
            return ""
        until = utc_now() + self.COOLDOWN
        what = livemod.OFF_HOURS_CODES[code]
        self.cooldowns[sym] = {"symbol": sym, "until": until, "code": code, "reason": what}
        return f"; {sym} cooling down until {until:%H:%M} UTC ({what}, {code})"

    def _cooldown(self, sym: str) -> Optional[str]:
        c = self.cooldowns.get(sym)
        if not c:
            return None
        if c["until"] <= utc_now():
            self.cooldowns.pop(sym, None)
            return None
        return f"{sym} cooling down until {c['until']:%H:%M} UTC after exchange error {c['code']} ({c['reason']})"

    def active_cooldowns(self) -> list[dict]:
        for s in list(self.cooldowns):
            self._cooldown(s)
        return [dict(c, until=c["until"].isoformat()) for c in self.cooldowns.values()]

    def _session_expired(self, err: Any) -> None:
        """baw session gone (can silently expire within 48h): pause trading + alert. Exits are still attempted."""
        if not self.control().paused:
            self._control_changed(self.c.risk.pause("Wallet session expired: re-scan the baw QR code, then resume"))
        self._alert(AlertLevel.CRITICAL, "WALLET_SESSION_EXPIRED", f"wallet session expired ({str(err)[:120]}); trading paused - "
                    "run `baw auth signin` (scan the QR), then POST /api/control/resume")

    # ------------------------------------------------------------------ in-flight LIVE buys (real-money safety)
    def _executor(self) -> Any:
        return getattr(self.c.exchange, "executor", None)

    def _pending_for(self, sym: Optional[str]) -> list[dict]:
        return [p for p in self.chain_pending.values() if sym is None or p["symbol"] == sym]

    def _add_pending(self, res: ExecutionResult, notional: float, quantity: Optional[float], price: Optional[float],
                     strategy: str, signal: Optional[Signal] = None, verdict: Optional[RiskVerdict] = None,
                     base: Optional[dict] = None, decision_id: Optional[str] = None) -> dict:
        px = price or self.book.prices.get(res.symbol)
        if not px:
            try:
                px = float(self.c.exchange.get_price(res.symbol))
            except Exception:
                px = None
        qty = quantity or (notional / px if px else 0.0)
        p = {"res": res, "client_order_id": res.client_order_id, "symbol": res.symbol, "notional": float(notional),
             "quantity": float(qty), "price": float(px or (notional / qty if qty else 0.0)), "strategy": strategy,
             "signal": signal, "verdict": verdict, "base": base, "decision_id": decision_id, "since": utc_now(),
             "held_exit": None}
        self.chain_pending[res.client_order_id] = p
        return p

    def _sync_intents(self) -> None:
        """Open BUY intents in Bot 4's journal (~/.teamwork/baw_intents.json, via executor.intents) that the loop does
        not know about (e.g. after a restart) become in-flight positions too."""
        intents = getattr(self._executor(), "intents", None)
        fn = getattr(intents, "open_intents", None)
        if not callable(fn):
            return
        try:
            recs = fn() or []
        except Exception:
            return
        for r in recs:
            cid = r.get("client_order_id") if isinstance(r, dict) else None
            if (not cid or cid in self.chain_pending or cid in self._settled or str(r.get("side", "BUY")).upper() != "BUY"
                    or not r.get("symbol")):
                continue
            try:
                notional = float(r.get("from_qty") or 0.0)
            except (TypeError, ValueError):
                notional = 0.0
            if notional <= 0:
                continue
            res = ExecutionResult(client_order_id=cid, symbol=r["symbol"], side=OrderSide.BUY, status=OrderStatus.PENDING_CONFIRMATION,
                                  path=OrderRequest.model_fields["path"].default, mode=self.s.trading_mode,
                                  notional=notional, tx_hash=r.get("tx_hash"), error="[intent_open] restored from the intent journal",
                                  **({"order_id": str(r["order_id"])} if r.get("order_id") else {}))
            self._add_pending(res, notional, None, None, self.active_strategy or "MANUAL:restored")

    def _synthetic_positions(self) -> list[Position]:
        out = []
        for cid, p in self.chain_pending.items():
            if p["quantity"] <= 0 or p["price"] <= 0:
                continue
            x = self._exit_rules()
            px = p["price"]
            out.append(Position(id=f"pending-{cid}", symbol=p["symbol"], quantity=p["quantity"], avg_entry_price=px,
                                opened_at=p["since"], strategy=p["strategy"], current_price=self.book.prices.get(p["symbol"]) or px,
                                highest_price=px, stop_loss_price=px * (1 - x.stop_loss_pct / 100),
                                take_profit_price=px * (1 + x.take_profit_pct / 100),
                                trailing_stop_pct=x.trailing_stop.pct if x.trailing_stop.enabled else None))
        return out

    def _pf(self) -> Portfolio:
        """The book + every in-flight BUY as a synthetic open position at its quote amount (cash already committed).
        Passed to BOTH the engine and risk, so a pending buy counts toward exposure / max positions / no second BUY."""
        pf = self.book.portfolio()
        syn = self._synthetic_positions()
        if not syn:
            return pf
        spent = sum(p["notional"] for p in self.chain_pending.values())
        return pf.model_copy(update={"positions": list(pf.positions) + syn, "cash": max(0.0, pf.cash - spent)})

    def _is_pending_target(self, sig: Signal) -> bool:
        if sig.position_id:
            return sig.position_id.startswith("pending-")
        return bool(self._pending_for(sig.symbol)) and not self.book.lots_for(sig.symbol)

    def _hold_exit(self, sig: Optional[Signal], source: str, regime: Optional[Regime], entry: Optional[dict] = None) -> Decision:
        """A SELL (engine / check_exits / emergency stop) aimed at a position still settling on chain is held; it is
        re-evaluated on the real position in the tick the fill is confirmed, and dropped if the buy fails."""
        if entry is not None:
            entries = [entry]
        elif sig is not None and sig.position_id and sig.position_id.startswith("pending-"):
            entries = [p for p in self.chain_pending.values() if f"pending-{p['client_order_id']}" == sig.position_id]
        else:
            entries = self._pending_for(sig.symbol if sig else None)
        why = (exit_reason_of(sig).value if sig is not None and exit_reason_of(sig) else None) or \
              ("EMERGENCY_STOP" if source == "emergency_stop" else (sig.reason if sig is not None else "exit"))
        for p in entries:
            p["held_exit"] = {"code": PENDING_EXIT, "reason": why, "source": source}
        sym = entries[0]["symbol"] if entries else (sig.symbol if sig else self.symbol)
        strategy = (sig.strategy if sig else None) or (entries[0]["strategy"] if entries else "NONE")
        if sig is None:
            sig = Signal(symbol=sym, action=Action.SELL, source=self.agent_mode or AgentMode.MANUAL, strategy=strategy,
                         reason="emergency stop", exit_reason=ExitReason.EMERGENCY_STOP, timestamp=self._now())
        sig = sig.model_copy(update={"conditions_failed": [*(sig.conditions_failed or []), PENDING_EXIT]})
        return self._finalize(Decision(
            mode=self.s.trading_mode, agent_mode=self.agent_mode, symbol=sym, regime=regime, timestamp=self._now(),
            strategy=strategy, signal=sig, action=Action.STAY_OUT,
            reason=f"exit held: buy still settling on-chain [{PENDING_EXIT}] ({why} via {source})"))

    def _settle_from_intent(self, res: ExecutionResult, rec: dict, p: dict) -> ExecutionResult:
        st = str(rec.get("state") or "").upper()
        upd: dict = {"tx_hash": rec.get("tx_hash") or res.tx_hash}
        if rec.get("order_id"):
            upd["order_id"] = str(rec["order_id"])
        if st == "FINISHED":
            try:
                qty = float(rec.get("to_qty") or 0) or p["quantity"]
            except (TypeError, ValueError):
                qty = p["quantity"]
            upd.update(status=OrderStatus.FILLED, filled_quantity=qty, notional=p["notional"],
                       avg_price=p["notional"] / qty if qty else None, error=None)
        else:
            upd.update(status=OrderStatus.FAILED, error=f"[failed] on-chain order {st or 'FAILED'} (reconcile)")
        return res.model_copy(update=upd)

    def _poll_chain_pending(self) -> list[Decision]:
        """In-flight BUYs: executor.reconcile() every tick (Bot 4, duck-typed) + client refresh_execution /
        get_execution_status. FILLED -> the synthetic position becomes the real lot (and an emergency-stop exit held
        meanwhile sells at once); FAILED -> it is dropped together with any held exit."""
        out: list[Decision] = []
        if not self.sim:
            self._sync_intents()
        if not self.chain_pending:
            return out
        ex = self.c.exchange
        recs: dict[str, dict] = {}
        rec_fn = getattr(self._executor(), "reconcile", None)
        if callable(rec_fn):
            try:
                for r in rec_fn() or []:
                    if isinstance(r, dict) and r.get("client_order_id"):
                        recs[r["client_order_id"]] = r
            except Exception as e:
                self.last_error = f"reconcile: {type(e).__name__}: {e}"
        fn = getattr(ex, "refresh_execution", None) or getattr(ex, "get_execution_status", None)
        for cid, p in list(self.chain_pending.items()):
            res = p["res"]
            rec = recs.get(cid)
            if rec and str(rec.get("state") or "").upper() in ("FINISHED", "FAILED"):
                res = self._settle_from_intent(res, rec, p)
            elif callable(fn):
                try:
                    res = fn(res)
                except Exception:
                    continue
            if livemod.is_waiting_on_chain(res):
                if (utc_now() - p["since"]).total_seconds() > 1800 and not p.get("alerted"):
                    p["alerted"] = True
                    self._alert(AlertLevel.WARNING, "CHAIN_PENDING", f"{res.symbol} BUY still unconfirmed after 30 min (tx {res.tx_hash})", res.symbol)
                continue
            self.chain_pending.pop(cid, None)
            self._settled.add(cid)
            sig, did = p["signal"], p["decision_id"] or Decision.model_fields["id"].default_factory()
            base = dict(p["base"] or dict(mode=self.s.trading_mode, agent_mode=self.agent_mode, symbol=res.symbol,
                                          regime=self.current_regime, strategy=p["strategy"], signal=sig), timestamp=self._now())
            self._remember_meta(res)
            livemod.record_execution(self.c.risk, res, quote_notional=p["notional"])  # final result, same client_order_id
            held = p.get("held_exit")
            why = sig.reason if sig else "restored in-flight order"
            if res.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED) and res.filled_quantity > 0:
                self._record_live_buy(res)
                spent = p["notional"] if res.status == OrderStatus.FILLED else res.notional + res.fee
                self.book.open_lot(res, spent, p["strategy"], self._exit_rules(), self._now())
                out.append(self._finalize(Decision(id=did, action=Action.BUY, risk_verdict=p["verdict"], execution=res,
                                                   reason=f"confirmed on chain; {why}", **base)))
                self.bus.publish(EventType.EXECUTION, res, decision_id=did)
                if held and (held.get("source") == "emergency_stop" or self.control().emergency_stopped):
                    out.append(self.close_symbol(res.symbol, ExitReason.EMERGENCY_STOP))  # sells right away
            else:
                note = f"; held exit dropped ({held['reason']})" if held else ""
                out.append(self._finalize(Decision(id=did, action=Action.STAY_OUT, risk_verdict=p["verdict"], execution=res,
                                                   reason=f"BUY failed on chain ({res.status.value}{': ' + res.error if res.error else ''}){note}", **base)))
                self.bus.publish(EventType.EXECUTION, res, decision_id=did)
        return out

    def portfolio_view(self) -> dict:
        """/api/portfolio and STATUS payloads (Bot 1): positions carry pending / client_order_id / held_exit."""
        pf = self._pf()
        d = pf.model_dump(mode="json")
        by_id = {f"pending-{cid}": p for cid, p in self.chain_pending.items()}
        for pos in d["positions"]:
            p = by_id.get(pos["id"])
            pos["pending"] = p is not None
            pos["client_order_id"] = p["client_order_id"] if p else None
            pos["held_exit"] = p.get("held_exit") if p else None
        return d

    def _remember_meta(self, res: ExecutionResult, quote: Optional[Quote] = None) -> None:
        m: dict = {}
        if quote is not None:
            m["quote_expires_at"] = quote.expires_at.isoformat()
            imp = livemod.quote_price_impact(quote)
            if imp is not None:
                m["price_impact_pct"] = imp
        extras = livemod.executor_extras(self.c.exchange, res.client_order_id)
        if extras:
            m["extras"] = extras
        if not m:
            return
        for k in {res.order_id, res.client_order_id} - {None}:
            self.exec_meta[k] = {**self.exec_meta.get(k, {}), **m}
        while len(self.exec_meta) > 5000:
            self.exec_meta.popitem(last=False)

    def _update_market_context(self, sym: str, pf) -> None:
        """Bot 2 live engine: per-order cap from risk, and set_market_context(symbol, {status, reference_price_only,
        min_order_usdt, priceImpactPercent, equity}) from token status + a probe quote; None when nothing is known.
        min_order_usdt is get_token_status(sym).min_order_usdt (None when unavailable), the same floor risk uses."""
        eng = getattr(self.c.engine, "_primary", None) or self.c.engine
        cfg = getattr(eng, "live_config", None)
        if cfg is not None and hasattr(cfg, "max_order_usdt"):
            cap = livemod.risk_live_limits(self.c.risk, pf, livemod.make_live_context(
                wallet_limits=self.wallet_settings.get())).get("per_order_cap_usdt")
            if cap and cap != cfg.max_order_usdt:
                try:
                    import dataclasses
                    eng.live_config = dataclasses.replace(cfg, max_order_usdt=float(cap))
                except Exception:
                    pass
        setter = getattr(eng, "set_market_context", None)
        if not callable(setter):
            return
        ex, ctx = self.c.exchange, {}
        fn = getattr(ex, "get_token_status", None)
        if callable(fn):
            try:
                st = fn(sym)
                d = st if isinstance(st, dict) else (st.model_dump() if hasattr(st, "model_dump") else vars(st))
                status = d.get("trading_status", d.get("status"))
                ctx["status"] = getattr(status, "value", status)  # enum -> 'OFFHOURS' (str(enum) is 'TradingStatus.X')
                ctx["reference_price_only"] = bool(d.get("reference_price_only", False))
                mo = d.get("min_order_usdt")
                ctx["min_order_usdt"] = float(mo) if mo is not None else None  # same minimum risk reads
            except Exception:
                ctx["min_order_usdt"] = None
        else:
            ctx["min_order_usdt"] = None
        if self._cooldown(sym):
            ctx["status"] = "closed"
        else:
            try:
                cap = getattr(getattr(eng, "live_config", None), "max_order_usdt", None) or self.s.live_max_order_usdt
                imp = livemod.quote_price_impact(ex.get_quote(sym, OrderSide.BUY, float(cap)))
                if imp is not None:
                    ctx["priceImpactPercent"] = imp
            except Exception as e:
                if self._maybe_cooldown(sym, e):
                    ctx["status"] = "closed"
        try:
            bal = livemod.read_exchange_wallet(ex)[1]
            if bal is not None:
                ctx["equity"] = bal.get(livemod.QUOTE_ASSET, 0.0)
        except Exception as e:
            if livemod.is_session_expired(e):
                self._session_expired(e)
        try:
            setter(sym, ctx or None)
        except Exception:
            pass

    def token_status(self, symbol: str) -> Optional[dict]:
        """Bot 4 get_token_status(symbol) as JSON (duck-typed), cached ~60 s per symbol; None when unavailable."""
        now = time.monotonic()
        hit = self._token_status_cache.get(symbol)
        if hit and now - hit[0] < 60:
            return hit[1]
        fn = getattr(self.c.exchange, "get_token_status", None)
        val: Optional[dict] = None
        if callable(fn):
            try:
                st = fn(symbol)
                if st is not None:
                    val = st.model_dump(mode="json") if hasattr(st, "model_dump") else (dict(st) if isinstance(st, dict) else dict(vars(st)))
            except Exception:
                val = None
        self._token_status_cache[symbol] = (now, val)
        return val

    def daily_live_notional(self) -> float:
        """Today's LIVE BUY notional: settled fills + in-flight buys."""
        done = float((self.state.get("live_bought") or {}).get(utc_now().date().isoformat(), 0.0))
        return done + sum(p["notional"] for p in self.chain_pending.values())

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
            focus = self.symbol or self.s.default_agent_symbol  # active (or default) token: its minimum drives min_wallet_usdt
            ctx = livemod.make_live_context(wallet_limits=wl, balances=bal if connected else None,
                                            daily_live_notional_usdt=self.daily_live_notional(),
                                            token_status=self._risk_token_status(focus))
            rl = livemod.risk_live_limits(self.c.risk, pf, ctx)
            per_token = self._per_token_limits(wl, bal if connected else None)
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
                      # wallet-side restriction from risk/baw only (None = none); never filled from the exchange list
                      "allowed_tokens": rl.get("allowed_tokens"),
                      "tradable_tokens": self.tradable_tokens(),  # app list for activation/UI: exchange tokens
                      "limit_source": rl.get("limit_source") or "app",
                      "warnings": list(rl.get("warnings") or [])}
            self._alert_new_warnings(limits["warnings"])
            self._sync_explainer_limits(limits)
            limits["limits_symbol"] = focus
            if per_token:
                limits["per_token"] = per_token
            limits.setdefault("daily_loss_cap_usdt", self.s.live_max_daily_loss_usdt)
            limits.setdefault("max_exposure_usdt", self.s.live_max_exposure_usdt)
            return {
                "provider": livemod.wallet_provider(self.c.exchange, self.s.trading_mode), "address": address, "chain": "BSC",
                "chain_id": livemod.BSC_CHAIN_ID, "connected": connected, "balances": rows,
                "bnb_gas_reserve": rl.get("bnb_gas_reserve", self.s.live_min_bnb), "limits": limits, "updated_at": utc_now(),
            }

    def _risk_token_status(self, symbol: Optional[str]) -> Any:
        """Bot 4 token status for `symbol` as Bot 3's TokenStatus (None when unknown / unconvertible)."""
        if not symbol:
            return None
        try:
            return livemod.to_token_status(self.token_status(symbol))
        except Exception:
            return None

    def _per_token_limits(self, wallet_limits: Any, balances: Optional[dict]) -> dict:
        """limits.per_token = {symbol: {min_order_usdt, min_wallet_usdt, min_wallet_usdt_note}} for every tradable token,
        from risk.min_wallet_info(LiveContext(token_status=...)) when risk has it, else Bot 4's min_order_usdt only."""
        fn = getattr(self.c.risk, "min_wallet_info", None)
        out: dict = {}
        for sym in self.tradable_tokens():
            raw = self.token_status(sym)
            if raw is None:
                continue
            row: dict = {"min_order_usdt": raw.get("min_order_usdt")}
            if callable(fn):
                try:
                    ctx = livemod.make_live_context(wallet_limits=wallet_limits, balances=balances,
                                                    token_status=livemod.to_token_status(raw))
                    row.update(dict(fn(ctx)))
                except Exception:
                    pass
            out[sym] = row
        return out

    def _sync_explainer_limits(self, limits: Optional[dict] = None) -> None:
        """Bot 5 set_live_limits(risk.live_limits()) so explanations can give small-wallet / gas hints (duck-typed)."""
        fn = getattr(self.c.explainer, "set_live_limits", None)
        if not callable(fn):
            return
        try:
            if limits is None:
                ctx = livemod.make_live_context(wallet_limits=self.wallet_settings.get() if self.live else None,
                                                daily_live_notional_usdt=self.daily_live_notional(),
                                                token_status=self._risk_token_status(self.symbol))
                limits = dict(livemod.risk_live_limits(self.c.risk, self.book.portfolio(), ctx))
                limits.setdefault("per_order_cap_usdt", self.s.live_max_order_usdt)  # app caps when risk has none
                limits.setdefault("daily_limit_usdt", self.s.live_max_daily_notional_usdt)
                limits.setdefault("bnb_gas_reserve", self.s.live_min_bnb)
            fn(limits)
        except Exception:
            pass

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

    def tradable_tokens(self) -> list[str]:
        """Tokens the agent may be activated on: the exchange token list (minus xStock in LIVE), or LIVE_ALLOWED_TOKENS.
        Separate from risk's / the wallet's allowed_tokens."""
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
                "risk_allow_live": allow, "wallet_connected": connected, "can_trade": not reasons, "blocked_reasons": reasons,
                "cooldowns": self.active_cooldowns(),
                "chain_pending": [{"order_id": v["res"].order_id, "client_order_id": k, "symbol": v["symbol"],
                                   "notional": v["notional"], "tx_hash": v["res"].tx_hash, "since": v["since"].isoformat(),
                                   "held_exit": v.get("held_exit")} for k, v in self.chain_pending.items()]}

    def enrich_payload(self, payload: Any) -> Any:
        out = livemod.enrich_payload(payload, self.exec_meta.get)
        if isinstance(out, dict) and isinstance(out.get("portfolio"), dict) and "control" in out:
            out["portfolio"] = self.portfolio_view()  # STATUS (HTTP + WS): pending flags on positions
        return out

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
        elif not agent:  # full autonomy by default: confirmation only when an activation explicitly asks for it
            try:
                self.c.risk.set_confirmation_mode(False)
            except Exception:
                pass
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
                fn = ex.execute_rfq if quote.path == ExecutionPath.RFQ else ex.execute_swap
                slip = self._slippage_cap() if self.live else None
                if slip is not None and "max_slippage_pct" in supported_kwargs(fn, {"max_slippage_pct": slip}):
                    res = fn(order, quote, max_slippage_pct=slip)  # Bot 4 a34f988: risk's cap per order
                else:
                    if slip is not None:
                        self._cap_slippage(slip)  # fallback for clients without the argument
                    res = fn(order, quote)
            except Exception as e:  # never let an exchange error kill the loop
                if livemod.is_session_expired(e):
                    self._session_expired(e)
                res = ExecutionResult(client_order_id=decision_id, symbol=sym, side=side, status=OrderStatus.FAILED,
                                      path=OrderRequest.model_fields["path"].default, mode=mode, error=f"{type(e).__name__}: {e}")
            finally:
                if fill_price and self._bar is not None:
                    self._set_sim_price(sym, self._bar.close)
        self._remember_meta(res, quote)
        # once at submission (incl. PENDING_CONFIRMATION), with the quote amount; the final result follows after reconcile
        livemod.record_execution(self.c.risk, res, quote_notional=quote.notional if quote is not None else None)
        if not self.sim:
            self.wallet_settings.invalidate()  # wallet quota changed: re-read baw settings before the next trade
            if res.error and livemod.is_session_expired(res.error):
                self._session_expired(res.error)
        if self.live and side == OrderSide.BUY and res.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
            self._record_live_buy(res)
        self.bus.publish(EventType.EXECUTION, res, decision_id=decision_id)
        if not (livemod.is_waiting_on_chain(res) or livemod.result_reason(res) == ORDER_IN_FLIGHT):
            self._publish_alerts(self.alerts.on_execution(res))  # in flight is not a failure
        return res

    def _slippage_cap(self) -> float:
        """Risk's LIVE slippage cap (percent): risk.live_limits()['max_slippage_pct'], else RISK_LIVE_MAX_SLIPPAGE_PCT."""
        rl = livemod.risk_live_limits(self.c.risk, self.book.portfolio(), None)
        return float(rl.get("max_slippage_pct") or self.s.live_max_quote_deviation_pct)

    def _cap_slippage(self, cap: float) -> None:
        """Fallback only (execute_* without max_slippage_pct): lower the executor default to risk's cap."""
        execu = self._executor()
        if execu is None or not hasattr(execu, "max_slippage_pct"):
            return
        try:
            cur = execu.max_slippage_pct
            execu.max_slippage_pct = float(cap) if cur is None else min(float(cur), float(cap))
        except Exception:
            pass

    def _buy(self, signal: Signal, verdict: RiskVerdict, base: dict, decision_id: Optional[str] = None) -> Decision:
        did = decision_id or Decision.model_fields["id"].default_factory()
        notional = verdict.adjusted_notional or signal.proposed_notional
        if notional <= 0:
            return self._finalize(Decision(id=did, action=Action.STAY_OUT, risk_verdict=verdict, reason="BUY approved with zero size", **base))
        # quote first in every mode: market closed / no liquidity / xStock disabled / below minimum is a normal
        # no-trade decision, never a FAILED execution
        try:
            quote = self.c.exchange.get_quote(signal.symbol, OrderSide.BUY, notional)
        except Exception as e:
            if livemod.is_session_expired(e):
                self._session_expired(e)
            note = self._maybe_cooldown(signal.symbol, e)
            return self._finalize(Decision(id=did, action=Action.STAY_OUT, risk_verdict=verdict,
                                           reason=f"BUY not executed: no quote ({_quote_error(e)}){note}", **base))
        if self.live:  # Bot 3 pass 2: re-check with the quote for the approved notional and a fresh `now`
            v2 = self._risk_check(signal, self._pf(), self.control(), quote=quote)
            if not v2.approved:
                return self._finalize(Decision(id=did, action=Action.STAY_OUT, risk_verdict=v2,
                                               reason="BUY not executed: " + _blocked(v2.reasons), **base))
            verdict = v2
        res = self._execute(OrderSide.BUY, signal.symbol, notional, did, quote=quote)
        if livemod.result_reason(res) == ORDER_IN_FLIGHT:  # Bot 4 single-flight: an earlier order is unresolved
            sig = base.get("signal")
            if sig is not None:
                base = {**base, "signal": sig.model_copy(update={"conditions_failed": [*(sig.conditions_failed or []), ORDER_IN_FLIGHT]})}
            return self._finalize(Decision(id=did, action=Action.STAY_OUT, risk_verdict=verdict,
                                           reason=f"BUY not executed: {ORDER_IN_FLIGHT} (an earlier LIVE order is unresolved: {res.error})", **base))
        if not self.sim and livemod.is_waiting_on_chain(res):  # submitted; the lot opens once the chain confirms
            self._add_pending(res, quote.notional if quote else notional, quote.quantity if quote else None,
                              quote.price if quote else None, signal.strategy, signal, verdict, base, did)
            return self._finalize(Decision(id=did, action=Action.BUY, risk_verdict=verdict, execution=res,
                                           reason=f"BUY submitted, waiting on chain{' (tx ' + res.tx_hash + ')' if res.tx_hash else ''}; {signal.reason}", **base))
        if res.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED) and res.filled_quantity > 0:
            spent = notional if res.status == OrderStatus.FILLED else res.notional + res.fee
            self.book.open_lot(res, spent, signal.strategy, self._exit_rules(), self._now())
            return self._finalize(Decision(id=did, action=Action.BUY, risk_verdict=verdict, execution=res,
                                           reason=signal.reason, **base))
        note = self._maybe_cooldown(signal.symbol, res.error) if res.error else ""
        return self._finalize(Decision(id=did, action=Action.STAY_OUT, risk_verdict=verdict, execution=res,
                                       reason=f"BUY not filled ({res.status.value}{': ' + res.error if res.error else ''}){note}; {signal.reason}", **base))

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

    def _pending_buy_for(self, sym: str) -> Optional[Pending]:
        """The pending (awaiting approval) BUY for this symbol under the current agent, if any."""
        return next((p for p in self.pending.values()
                     if p.decision.symbol == sym and p.decision.action == Action.BUY
                     and p.decision.agent_mode == self.agent_mode), None)

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
            verdict = self._risk_check(p.signal, self._pf(), control)  # re-check at approval time
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
        """The toggle also sets the active agent's activation (OFF -> AUTO_EXECUTE, ON -> CONFIRMATION), so a strategy
        activated with CONFIRMATION doesn't keep requiring approval after the user switched confirmation off."""
        with self.lock:
            act = ActivationMode.CONFIRMATION if enabled else ActivationMode.AUTO_EXECUTE
            if self.manual is not None and self.manual.activation != act:
                self.manual = self.manual.model_copy(update={"activation": act})
                hook = getattr(self.c.risk, "apply_manual_strategy", None)  # risk reads activation per symbol
                if callable(hook):
                    try:
                        hook(self.manual)
                    except Exception as e:
                        self._alert(AlertLevel.WARNING, "CONFIRMATION_MODE", f"risk could not apply the new activation: {e}")
                self.state.update(agent={"kind": "manual", "strategy": self.manual.model_dump(mode="json")})
            elif self.auto is not None and self.auto.activation != act:
                self.auto = self.auto.model_copy(update={"activation": act})
                self.state.update(agent={"kind": "auto", "config": self.auto.model_dump(mode="json")})
            return self._control_changed(self.c.risk.set_confirmation_mode(enabled))

    def emergency_stop(self, reason: str = "", flatten: bool = False) -> ControlState:
        with self.lock:
            ctrl = self.c.risk.emergency_stop(reason)
            self._reject_all_pending("emergency stop")
            if flatten:
                for sym in sorted({l.position.symbol for l in self.book.lots}):
                    self.close_symbol(sym, ExitReason.EMERGENCY_STOP)
            for p in self._pending_for(None):  # in-flight buys: sold the moment the fill is confirmed
                self._hold_exit(None, "emergency_stop", self.current_regime, entry=p)
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
            subject = d
            if self.live and d.execution is not None:  # result + executor extras (explorer_url, route, price impact)
                from .schemas import DecisionView
                subject = DecisionView.model_validate(self.enrich_payload(d.model_dump(mode="json")))
            d = d.model_copy(update={"explanation": self.c.explainer.explain_decision(subject)})
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
