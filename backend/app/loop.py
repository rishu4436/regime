"""The trading loop: control -> (protective exits) -> regime -> strategy -> risk -> execute/confirm -> explain -> log.

Built only against the contract Protocols (engine / risk / exchange / explainer). One active agent at a time:
either a ManualStrategy or an AutoConfig. `tick()` runs exactly one cycle on the next bar; the API runs it on a
timer (`/api/loop/start`) or on demand (`/api/loop/tick`, used by the e2e tests).
"""
from __future__ import annotations

import logging
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
from backend.contracts import SIM_WARMUP_BARS, AlertLevel
from backend.monitoring import AlertManager, EventLog, PerformanceTracker, one_prefix

from .events import EventBus
from .exits import check_protective_exit, exit_reason_of, trailing_level
from .feed import PollingFeed, ReplayFeed
from .fallbacks import DAILY_LOSS_LIMIT_PCT
from . import limits as limitsmod
from . import live as livemod
from .portfolio import Lot, PortfolioBook
from .settings import Settings
from .state import AgentStateStore, state_path
from .wiring import Components, call_with_supported, supported_kwargs

WALLET_BACKOFF_S = (60.0, 120.0, 300.0)  # LIVE wallet read failed for a network reason: next tries after 60 s, 120 s, then every 300 s
ORDER_READ_REUSE_S = 20.0  # the pre-order wallet read is reused for the rest of that order (risk pass 1 + pass 2)
log = logging.getLogger("backend.app.loop")

SESSION_PAUSE_REASON = "Wallet session expired: click Reconnect wallet"  # the pause this app sets (auto-cleared on sign-in)
SESSION_PAUSE_REASONS = (SESSION_PAUSE_REASON, "Wallet session expired: re-scan the baw QR code, then resume")
PENDING_EXIT = "pending_exit"        # exit held while the BUY settles on chain (Bot 1 / Bot 5 code)
ORDER_IN_FLIGHT = "order_in_flight"  # Bot 4 executor single-flight refusal (verbatim)


def _short_addr(addr: Optional[str]) -> str:
    """0xabcdef...1234 -> '0xabcd\u20261234' (first 6 chars + ellipsis + last 4) for user-facing text."""
    a = str(addr or "").strip()
    if not a:
        return "unknown"
    return a if len(a) <= 10 else f"{a[:6]}\u2026{a[-4:]}"


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
        self._boot_wallet: Optional[dict] = None
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
        # LIVE wallet session from the LATEST wallet read (each tick, /api/wallet, /api/live/status; Bot 4 caches <= 15 s)
        self.wallet_session: dict = {"expired": False, "since": None, "error": None, "checked_at": None, "exit_alerted": False}
        # LIVE wallet cache (v0.9, fewer baw calls): GET /api/wallet and /api/live/status serve the LAST read and never call
        # baw. The loop re-reads every WALLET_REFRESH_SEC and right before every order; a network failure keeps the last
        # good read (stale: BUYs blocked, exits still go out) and backs off 60/120/300 s.
        self.wallet_refresh_s = max(1.0, float(getattr(settings, "wallet_refresh_sec", 300.0) or 300.0))
        self.wcache: dict = {"state": None, "read_at": None, "attempt_at": None, "stale": False, "error": None,
                             "failures": 0, "retry_at": None, "alerted": False}
        self._wallet_io = threading.Lock()
        self._order_memo: Optional[tuple] = None  # (monotonic, with_settings, state | None, detail, tick)
        self.bus.enrich = self.enrich_payload
        self.wallet_settings = livemod.WalletSettingsProvider(
            comps.exchange, settings.trading_mode, on_error=lambda m: self._alert(AlertLevel.WARNING, "WALLET_SETTINGS", m))
        if self._boot_wallet is not None:  # the startup read (balances) + the wallet's own limits once
            self.wcache.update(state=self._boot_wallet, read_at=utc_now(), attempt_at=utc_now())
            self.wallet_settings.get(force=True)
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
        self._awaiting: "OrderedDict[str, str]" = OrderedDict()  # STAY_OUT decision id -> pending BUY decision id
        self.wallet_block: Optional[dict] = None  # {address, expected, since}: a different wallet signed in (v0.8)
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
        self._init_limits()
        self.picks: list[dict] = []
        self.picks_skipped: list[dict] = []
        self.picks_explanations: list[str] = []
        self.picks_at, self.picks_tick, self.picks_updated = 0.0, 0, None
        self._last_by_symbol: dict[str, Any] = {}
        self._pick_candle_cache: dict = {}
        self._restore()
        if not self.sim:  # the curve starts at the SAME baseline as /api/portfolio (restored state / wallet USDT)
            self._rebase_performance()

    def _rebase_performance(self) -> None:
        """LIVE/PAPER: equity curve, return % and drawdown measured from book.starting_capital (what /api/portfolio
        shows), then the current equity. SIM keeps its configured capital and replay timeline."""
        pf = self.book.portfolio()
        now = utc_now()
        self.perf.rebase(now, self.book.starting_capital)
        if abs(pf.equity - self.book.starting_capital) > 1e-9:
            self.perf.record_equity(now + timedelta(microseconds=1), pf.equity)

    def _live_starting_capital(self, settings: Settings, comps: Components) -> float:
        """LIVE: start the book from the wallet's USDT balance when the exchange exposes one (never more than the
        configured STARTING_CAPITAL, so the risk % sizing stays tiny); otherwise STARTING_CAPITAL. A good read here also
        seeds the wallet cache (no second baw read at startup)."""
        if settings.trading_mode != TradingMode.LIVE:
            return settings.starting_capital
        st = livemod.read_exchange_wallet_state(comps.exchange)
        if st["connected"] and not st["session_expired"]:
            self._boot_wallet = st
        bal = st["balances"]
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

    # ------------------------------------------------------------------ watchlist + Auto-pick (v0.3)
    PICK_REFRESH_S = 600.0     # PAPER/LIVE: re-rank at most every 10 minutes (keeps Web3 API load low)
    PICK_REFRESH_TICKS = 24    # SIM: re-rank every 24 replay bars
    PICK_CANDIDATES = 20       # candles are fetched for at most this many pre-filtered tokens

    def _agent_cfg(self) -> Any:
        return self.manual or self.auto

    def _auto_pick_cfg(self) -> Any:
        ap = getattr(self._agent_cfg(), "auto_pick", None)
        return ap if ap is not None and ap.enabled else None

    def configured_watchlist(self) -> list[str]:
        cfg = self._agent_cfg()
        if cfg is None:
            return []
        wl = [s for s in dict.fromkeys([cfg.symbol] + list(getattr(cfg, "watchlist", None) or [])) if s]
        return wl[:5]

    def watch_symbols(self) -> list[str]:
        """Tokens that may get new BUYs this tick: the Auto-pick picks when Auto-pick is on and has picks, else the
        configured watchlist (symbol first). Always at least the agent's symbol."""
        if self._auto_pick_cfg() is not None and self.picks:
            return [p["symbol"] for p in self.picks][:5]
        return self.configured_watchlist()

    def _sync_replay_clock(self, sym: str) -> None:
        """SIM: a token joining mid-replay starts at the same replay time as the agent's symbol (one shared clock)."""
        clock = getattr(self.feed, "_clock", None)
        if isinstance(clock, dict) and sym not in clock and self.symbol in clock:
            clock[sym] = clock[self.symbol]

    def _narrative_of(self) -> Any:
        fn = getattr(getattr(self.c.explainer, "_primary", None), "narrative_of", None)
        if callable(fn):
            try:
                f = fn()
                if callable(f):
                    return f
            except Exception:
                pass
        try:
            from backend.llm.narratives import narrative_of
            return narrative_of
        except Exception:
            return None

    def token_rows(self, all_tokens: bool = False) -> list[dict]:
        """Bot 4 list_tokens() rows (full universe, cached by the client) or search_tokens('') as rows."""
        fn = getattr(self.c.exchange, "list_tokens", None)
        if all_tokens and callable(fn):
            try:
                return [dict(r) for r in fn()]
            except Exception as e:
                log.warning("list_tokens failed: %s", e)
        rows = []
        for t in self.c.exchange.search_tokens(""):
            d = t.model_dump(mode="json")
            st = self.token_status(t.symbol) or {}
            rows.append({**d, "type": d.get("token_type"), "status": st.get("trading_status"), "tradable": st.get("tradable"),
                         "min_order_usdt": st.get("min_order_usdt"), "max_order_usdt": st.get("max_order_usdt")})
        return rows

    def compute_picks(self, narrative: str = "ANY", max_tokens: int = 3) -> dict:
        """Bot 2's Auto-pick: list_tokens -> candidate_tokens (<= 20, no candles yet) -> candles for those only ->
        rank_tokens. Returns {picks:[{symbol, narrative, score, reason}], skipped:[{symbol, reason}], explanations}."""
        from backend.engine.autopick import AutoPickConfig as EngineAutoPick, candidate_tokens, rank_tokens
        nar = self._narrative_of()
        cap = float((self.user_limits or {}).get("max_order_usdt") or self.s.live_max_order_usdt)
        cfg = EngineAutoPick(max_order_usdt=cap)
        rows = self.token_rows(all_tokens=True)
        cands = candidate_tokens(rows, narrative, narrative_of=nar, limit=self.PICK_CANDIDATES, cfg=cfg)
        candles: dict[str, list] = {}
        for t in cands:
            sym = t["symbol"]
            try:
                candles[sym] = self._pick_candles(sym)
            except Exception as e:
                log.info("auto-pick: no candles for %s: %s", sym, e)
        res = rank_tokens(cands, candles, narrative, max_tokens, narrative_of=nar, cfg=cfg)
        d = res.to_dict()
        out = {"picks": d["picks"], "skipped": d["skipped"], "explanations": self._explain_picks(d["picks"], d["skipped"])}
        return out

    def _pick_candles(self, sym: str) -> list:
        """Candles for ranking, cached per bar: SIM up to the replay clock (never the future), else the exchange's."""
        key = (sym, self.timeframe)
        hit = self._pick_candle_cache.get(key)
        if hit and (time.monotonic() - hit[0]) < (self.PICK_REFRESH_S if not self.sim else 1e9) and (not self.sim or hit[2] == self.ticks):
            return hit[1]
        if self.sim and hasattr(self.feed, "series"):
            self._sync_replay_clock(sym)
            s = self.feed.series(sym, self.timeframe)
            clock = getattr(self.feed, "_clock", {}).get(self.symbol)
            cs = [c for c in s if clock is None or c.timestamp <= clock] or s[:SIM_WARMUP_BARS]
            cs = cs[-self.s.window_bars:]
        else:
            cs = list(self.c.exchange.get_candles(sym, self.timeframe, limit=self.s.window_bars))
        self._pick_candle_cache[key] = (time.monotonic(), cs, self.ticks)
        return cs

    def _explain_picks(self, picks: list, skipped: list) -> list[str]:
        for impl in (getattr(self.c.explainer, "_primary", None), self.c.explainer):
            fn = getattr(impl, "explain_picks", None)
            if callable(fn):
                try:
                    return [str(x) for x in fn(picks, skipped)]
                except Exception:
                    pass
        try:
            from backend.llm.narratives import explain_picks
            return list(explain_picks(picks, skipped))
        except Exception:
            return [p.get("reason") or p["symbol"] for p in picks]

    def refresh_picks(self, force: bool = False) -> None:
        ap = self._auto_pick_cfg()
        if ap is None:
            return
        now = time.monotonic()
        due = force or not self.picks_at or (self.ticks - self.picks_tick >= self.PICK_REFRESH_TICKS if self.sim
                                             else now - self.picks_at >= self.PICK_REFRESH_S)
        if not due:
            return
        try:
            r = self.compute_picks(ap.narrative, ap.max_tokens)
        except Exception as e:
            self._alert(AlertLevel.WARNING, "AUTO_PICK", f"Auto-pick failed ({type(e).__name__}: {e}); keeping the current watchlist")
            self.picks_at, self.picks_tick = now, self.ticks
            return
        before = [p["symbol"] for p in self.picks]
        self.picks, self.picks_skipped, self.picks_explanations = r["picks"], r["skipped"], r["explanations"]
        self.picks_at, self.picks_tick, self.picks_updated = now, self.ticks, utc_now()
        after = [p["symbol"] for p in self.picks]
        if after != before:
            self._alert(AlertLevel.INFO, "AUTO_PICK", ("Auto-pick: " + ", ".join(after)) if after else
                        "Auto-pick found no tradable token for this narrative; using the watchlist")

    def watchlist_view(self) -> dict:
        with self.lock:
            ap = getattr(self._agent_cfg(), "auto_pick", None)
            pf = self.book.portfolio()
            held = {p.symbol: p for p in pf.positions}
            per = []
            syms = self.watch_symbols() if self._agent_cfg() is not None else []
            for sym in syms + [s for s in held if s not in syms]:
                d = self._last_by_symbol.get(sym)
                p = held.get(sym)
                sig = getattr(d, "signal", None) if d else None
                per.append({"symbol": sym, "watched": sym in syms,
                            "regime": (d.regime.value if d and d.regime else None),
                            "signal": (sig.action.value if sig else None), "action": d.action.value if d else None,
                            "reason": d.reason if d else None, "held": p is not None,
                            "position": None if p is None else {"quantity": p.quantity, "avg_entry_price": p.avg_entry_price,
                                                                "value_usdt": p.quantity * (p.current_price or p.avg_entry_price)},
                            "position_usdt": None if p is None else p.quantity * (p.current_price or p.avg_entry_price)})
            return {"watchlist": syms, "configured_watchlist": self.configured_watchlist(),
                    "auto_pick": ap.model_dump(mode="json") if ap is not None else None,
                    "picks": list(self.picks), "skipped": list(self.picks_skipped),
                    "explanations": list(self.picks_explanations), "per_token": per,
                    "updated_at": self.picks_updated}

    # ------------------------------------------------------------------ activation
    def _check_switch(self, symbol: str, watchlist: Optional[list] = None) -> None:
        if watchlist is not None:  # watchlist agents keep other holdings: their exits keep running every tick
            return
        others = {l.position.symbol for l in self.book.lots if l.position.symbol != symbol}
        if others:
            raise AgentStateError(f"close open positions in {', '.join(sorted(others))} before switching symbol")

    def activate_manual(self, strategy: ManualStrategy) -> StatusSnapshot:
        with self.lock:
            self._check_switch(strategy.symbol, self._multi(strategy))
            self.manual, self.auto = strategy, None
            self.active_strategy = f"MANUAL:{strategy.name}"
            hook = getattr(self.c.risk, "apply_manual_strategy", None)  # optional (Bot 3): per-symbol risk/exit rules
            if callable(hook):
                hook(strategy)
                for sym in (strategy.watchlist or [])[:5]:  # same rules for every watched token
                    if sym != strategy.symbol:
                        hook(strategy.model_copy(update={"symbol": sym}))
            self._reset_picks()
            self._on_activate(strategy.symbol, strategy.timeframe, strategy.activation)
            self.state.update(agent={"kind": "manual", "strategy": strategy.model_dump(mode="json")})
            return self.status()

    def activate_auto(self, config: AutoConfig) -> StatusSnapshot:
        with self.lock:
            self._check_switch(config.symbol, self._multi(config))
            self.manual, self.auto = None, config
            self.active_strategy = None
            self._reset_picks()
            self._on_activate(config.symbol, config.timeframe, config.activation)
            self.state.update(agent={"kind": "auto", "config": config.model_dump(mode="json")})
            return self.status()

    @staticmethod
    def _multi(cfg: Any) -> Optional[list]:
        """The watchlist when the config uses one (2+ tokens or Auto-pick on), else None (single-token behaviour)."""
        wl = [s for s in dict.fromkeys([cfg.symbol] + list(getattr(cfg, "watchlist", None) or [])) if s]
        ap = getattr(cfg, "auto_pick", None)
        return wl if len(wl) > 1 or (ap is not None and ap.enabled) else None

    def _reset_picks(self) -> None:
        self.picks, self.picks_skipped, self.picks_explanations = [], [], []
        self.picks_at, self.picks_tick, self.picks_updated = 0.0, 0, None
        self._last_by_symbol = {}
        if self._auto_pick_cfg() is not None:
            self.refresh_picks(force=True)

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
        """One cycle over every watched token (watchlist / Auto-pick picks, max 5) plus every held token that left
        the watchlist (exits only). A single-symbol agent runs exactly as before."""
        with self.lock:
            if self.symbol is None:
                raise AgentStateError("no active agent: activate a manual strategy or auto config first")
            self.last_tick_at = utc_now()  # heartbeat: every cycle counts, even without a new bar
            if self.live:
                self.wallet_refresh_if_due()  # slow schedule (WALLET_REFRESH_SEC); orders re-read right before they go out
            if self._auto_pick_cfg() is not None:
                self.refresh_picks()  # at most every PICK_REFRESH_S / PICK_REFRESH_TICKS, never every tick
            watched = self.watch_symbols()
            held = [s for s in dict.fromkeys(l.position.symbol for l in self.book.lots) if s not in watched]
            out: list[Decision] = []
            progressed = False
            self._sync_explainer_limits()
            for sym in watched + held:
                res = self._tick_symbol(sym, entries=sym in watched, primary=sym == watched[0])
                if res is not None:
                    progressed = True
                    out.extend(res)
                    self._last_by_symbol[sym] = res[-1] if res else None
            if not progressed:
                return []
            self.ticks += 1
            return self._end_tick(out)

    def _tick_symbol(self, sym: str, entries: bool = True, primary: bool = True) -> Optional[list[Decision]]:
        """The single-token cycle for `sym` (None = no new bar). entries=False: a held token no longer watched -
        protective exits and the engine's SELL only, never a new BUY."""
        if True:
            if self.sim and not primary:
                self._sync_replay_clock(sym)
            window = self.feed.next_window(sym, self.timeframe)
            if window is None:
                if self.sim and primary and not self.finished:
                    self.finished = True
                    self._alert(AlertLevel.INFO, "REPLAY_FINISHED", f"SIM replay of {sym} reached the last bar", sym)
                return None
            bar = self._bar = window[-1]
            now = self._now()
            self._set_sim_price(sym, bar.close)
            self.book.on_bar(sym, bar, now)
            self.bus.publish(EventType.PRICE, {"symbol": sym, "price": bar.close, "candle": bar.model_dump(mode="json")})
            out: list[Decision] = []
            if self.wallet_block:  # a different wallet signed in: no orders at all (not even exits) until reconciled
                b = self.wallet_block
                out.append(self._finalize(Decision(
                    mode=self.s.trading_mode, agent_mode=self.agent_mode, symbol=sym, regime=self.current_regime, timestamp=now,
                    strategy=self.active_strategy or "NONE", action=Action.STAY_OUT,
                    reason=(f"Paused: a different wallet is connected ({_short_addr(b.get('address'))}, expected "
                            f"{_short_addr(b.get('expected'))}). Regime places no orders, and stop-loss and take-profit are "
                            "paused, until you click Check positions on the dashboard."))))
                return out

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
                return out

            # 3) strategy (engine) -> 4) risk -> 5) execute / confirm
            pf = self._pf()  # + in-flight buys as synthetic positions (engine and risk both see them)
            if not self.sim:
                self._update_market_context(sym, pf)
            if self.manual:
                strat = self.manual if self.manual.symbol == sym else self.manual.model_copy(update={"symbol": sym})
                signal = self.c.engine.evaluate_manual(strat, window, pf)
            else:
                cfg = self.auto if self.auto.symbol == sym else self.auto.model_copy(update={"symbol": sym})
                signal = self.c.engine.evaluate_auto(cfg, window, pf)
                self.active_strategy = signal.strategy if signal.preset else "NONE"
                if signal.regime is not None:
                    regime = self.current_regime = signal.regime
            base = dict(mode=self.s.trading_mode, agent_mode=self.agent_mode, symbol=sym, regime=regime, timestamp=now,
                        strategy=signal.strategy, signal=signal)
            if signal.action == Action.BUY and not entries:  # a dropped token: exits only, never a new BUY
                signal = signal.model_copy(update={"action": Action.STAY_OUT, "reason": (
                    f"{sym} is no longer on the watchlist: holding for exits only (stop-loss / take-profit / sell signal)")})
            if signal.action == Action.STAY_OUT:  # incl. Bot 2's live_* / insufficient_data: a logged no-trade
                out.append(self._finalize(Decision(action=Action.STAY_OUT, reason=signal.reason, **base)))
                return out
            cd = self._cooldown(sym) if signal.action == Action.BUY else None
            if cd:
                out.append(self._finalize(Decision(action=Action.STAY_OUT, reason=f"BUY not executed: {cd}", **base)))
                return out
            waiting = self._pending_buy_for(sym) if signal.action == Action.BUY else None
            if waiting is not None:  # one confirmation per symbol/agent at a time: no stale approval queue
                d = Decision(action=Action.STAY_OUT, reason=(
                    "BUY not executed: awaiting your approval of the pending buy; "
                    "no new order until you approve or reject it"), **base)
                self._awaiting[d.id] = waiting.decision.id  # structured: DecisionView.awaiting_decision_id
                while len(self._awaiting) > 2000:
                    self._awaiting.popitem(last=False)
                out.append(self._finalize(d))
                return out
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
            return out

    def _end_tick(self, out: list[Decision]) -> list[Decision]:
        self._persist()
        if self.live:  # surface Bot 3's limit warnings (e.g. quota can't cover selling all positions) as alerts
            try:
                ctx = livemod.make_live_context(wallet_limits=self.wallet_settings.peek(), daily_live_notional_usdt=self.daily_live_notional())
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
        # pass 2 (with the quote, right before the order goes out): a FRESH wallet read (balances + `baw wallet settings`),
        # so wallet_limits carry THAT read's own fetched_at and an old cached copy can never approve a BUY.
        # pass 1 (no quote yet): the loop's last read, no baw call (a BUY risk blocks anyway never costs a wallet read).
        # No good read (network failure, back-off) = no BUY.
        if quote is not None or self.wcache["read_at"] is None:
            st, why = self._order_wallet_read(with_settings=True)
        else:
            st, why = self._cached_wallet_state(), ""
            if self.wcache["stale"]:
                nxt = self.wcache["retry_at"]
                st, why = None, (f"wallet read failed ({self.wcache['error'] or 'network error'}); BUYs wait for a fresh "
                                 "wallet read" + (f" (next try {nxt:%H:%M:%S} UTC)" if nxt else ""))
        balances = st["balances"] if st is not None else None
        if self.wallet_session["expired"]:
            errs.append(RiskCheck(name="live_wallet_session", passed=False, detail=livemod.SESSION_EXPIRED_REASON))
        elif st is None:
            errs.append(RiskCheck(name="live_wallet_stale", passed=False, detail=why))
        elif balances is None:
            errs.append(RiskCheck(name="live_wallet_balances", passed=False,
                                  detail="wallet balances unavailable (exchange client has no get_wallet/get_balances): no BUYs"))
        extra: dict = {"wallet_limits": self.wallet_settings.peek() if st is not None else None}
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
        """baw session gone (can silently expire within 48h): pause trading + alert once per outage. Exits are HELD (no
        swaps that can only fail) and retried on the first tick after a successful wallet read (Reconnect wallet)."""
        ws = self.wallet_session
        first = not ws["expired"]
        ws.update(expired=True, error=str(err)[:200])
        if first:
            ws.update(since=utc_now(), exit_alerted=False)
            self._drop_wallet_cache()  # the next read is fresh, so recovery is seen as soon as the user signs in
        if not self.control().paused:
            self._control_changed(self.c.risk.pause(SESSION_PAUSE_REASON))
        if first:
            self._alert(AlertLevel.CRITICAL, "WALLET_SESSION_EXPIRED", f"wallet session expired ({str(err)[:120]}); trading "
                        "paused and exits held - click Reconnect wallet on the dashboard (or run `baw auth signin`)")

    def _session_restored(self, resume: bool = True) -> None:
        """A successful wallet read after an expired session: clear it (held exits retry), and when the wallet itself
        reported CONNECTED (`resume`) lift only the pause this app set for the session; alert."""
        ws = self.wallet_session
        was = ws["expired"]
        ws.update(expired=False, since=None, error=None, exit_alerted=False)
        ctrl = self.control()
        resumed = False
        # also a session pause restored from the state file after a restart (incl. the pre-v0.9 wording)
        if resume and ctrl.paused and not ctrl.emergency_stopped and (ctrl.reason or "") in SESSION_PAUSE_REASONS:
            self._control_changed(self.c.risk.resume())
            resumed = True
        if not (was or resumed):
            return
        self._alert(AlertLevel.INFO, "WALLET_SESSION_RESTORED", "wallet signed in again; " + (
            "trading resumed and held exits retry on the next check" if resumed else "held exits retry on the next check"))

    def _drop_wallet_cache(self) -> None:
        ex = self.c.exchange
        for obj in (ex, getattr(ex, "executor", None), getattr(ex, "_executor", None)):
            if obj is not None and hasattr(obj, "_wallet_cache"):
                try:
                    obj._wallet_cache = None
                except Exception:
                    pass

    # ------------------------------------------------------------------ LIVE wallet cache (v0.9)
    def _empty_wallet_state(self) -> dict:
        return {"address": getattr(self.c.exchange, "wallet_address", None), "balances": None, "connected": False,
                "session_expired": False, "verified": False, "reason": None, "error": None}

    def _cached_wallet_state(self) -> dict:
        """The last wallet read (never calls baw); session_expired follows the loop's session state."""
        st = dict(self.wcache["state"] or self._empty_wallet_state())
        st["session_expired"] = bool(st.get("session_expired") or self.wallet_session["expired"])
        if st["session_expired"]:
            st["connected"] = False
        return st

    def _wallet_read(self, with_settings: bool = True, user: bool = False) -> dict:
        """The ONLY place the LIVE wallet is read from the exchange (baw): balances + session, and with_settings also
        `baw wallet settings` (fresh fetched_at). Session transitions as before ('illegal parameter' = session expired).
        A network failure (ConnectError / getaddrinfo / timeout) does NOT expire the session: the last good read is kept
        and marked stale (BUYs blocked), and the next try backs off 60 s, 120 s, then 300 s. During a back-off this returns
        the cached read without calling baw, unless `user` (a wallet sign-in / reconcile the user just did)."""
        wc = self.wcache
        if not user and wc["retry_at"] is not None and utc_now() < wc["retry_at"]:
            return self._cached_wallet_state()
        if not self._wallet_io.acquire(timeout=60):  # another read is running: use its result
            return self._cached_wallet_state()
        settings_err: Optional[str] = None
        try:
            self._drop_wallet_cache()  # Bot 4's executor caches 15 s: this read must be fresh
            st = livemod.read_exchange_wallet_state(self.c.exchange)
            if with_settings and st["connected"] and not st["session_expired"]:
                self.wallet_settings.get(force=True)
                settings_err = self.wallet_settings.last_error
        finally:
            self._wallet_io.release()
        now = utc_now()
        with self.lock:
            wc["attempt_at"] = now
            self.wallet_session["checked_at"] = now
            if livemod.is_network_failure(st) or (settings_err and livemod.is_network_error_text(settings_err)):
                wc["failures"] += 1
                delay = WALLET_BACKOFF_S[min(wc["failures"], len(WALLET_BACKOFF_S)) - 1]
                err = str(st.get("error") or settings_err or st.get("reason") or "network error")[:200]
                wc.update(stale=True, error=err, retry_at=now + timedelta(seconds=delay))
                if not wc["alerted"]:
                    wc["alerted"] = True
                    self._alert(AlertLevel.WARNING, "WALLET_READ_FAILED", f"wallet read failed ({err[:120]}): not a session problem. "
                                f"Regime keeps the last wallet read and places no BUYs until a fresh read succeeds (next try in "
                                f"{delay:.0f}s); stop-loss and take-profit still go out")
                return self._cached_wallet_state()
            if wc["alerted"]:
                self._alert(AlertLevel.INFO, "WALLET_READ_OK", "wallet read works again; BUYs allowed")
            wc.update(state=st, read_at=now, stale=False, error=None, failures=0, retry_at=None, alerted=False)
            if st["session_expired"]:
                self._session_expired(st.get("error") or st.get("reason") or "session_expired")
            elif st["connected"]:
                self._session_restored(resume=bool(st.get("verified")))
        return st

    def wallet_refresh_if_due(self) -> bool:
        """LIVE slow schedule: re-read when the last read is WALLET_REFRESH_SEC old (or never read), or when a back-off
        after a network failure is over. Called each tick and by the app's monitor; usually a no-op (no baw call)."""
        if not self.live:
            return False
        with self.lock:  # lock order: self.lock -> _wallet_io (the tick holds self.lock while it reads)
            wc, now = self.wcache, utc_now()
            if wc["retry_at"] is not None:
                if now < wc["retry_at"]:
                    return False
            elif wc["read_at"] is not None and (now - wc["read_at"]).total_seconds() < self.wallet_refresh_s:
                return False
            self._wallet_read()
            return True

    def _order_wallet_read(self, with_settings: bool = True) -> tuple[Optional[dict], str]:
        """A fresh wallet read right before an order: (state, "") or (None, why) when no fresh read is possible (network
        failure / back-off). Reused within the same tick (max ORDER_READ_REUSE_S) so one order's two risk passes read baw once."""
        m = self._order_memo
        if m and m[4] == self.ticks and time.monotonic() - m[0] < ORDER_READ_REUSE_S and (m[1] or not with_settings):
            return m[2], m[3]
        wc = self.wcache
        before = wc["read_at"]
        st = self._wallet_read(with_settings=with_settings)
        if wc["read_at"] is not None and wc["read_at"] != before and not wc["stale"]:
            res: tuple[Optional[dict], str] = (st, "")
        else:
            nxt = wc["retry_at"]
            res = (None, f"wallet read failed ({wc['error'] or 'network error'}); BUYs wait for a fresh wallet read"
                         + (f" (next try {nxt:%H:%M:%S} UTC)" if nxt else ""))
        self._order_memo = (time.monotonic(), with_settings, *res, self.ticks)
        return res

    def wallet_cache_info(self) -> dict:
        wc, now = self.wcache, utc_now()
        ra = wc["read_at"]
        return {"read_at": ra, "age_s": round((now - ra).total_seconds(), 1) if ra else None, "stale": bool(wc["stale"]),
                "read_error": wc["error"], "next_retry_at": wc["retry_at"]}

    def _held_exit_session(self, signal: Signal, tag: str, base: dict, did: str) -> Decision:
        """Exit while the wallet session is expired: no swap (it can only fail); retried after Reconnect wallet."""
        if not self.wallet_session.get("exit_alerted"):
            self.wallet_session["exit_alerted"] = True
            self._alert(AlertLevel.CRITICAL, "EXIT_HELD_SESSION", f"{tag} exit for {signal.symbol} is held until the wallet is "
                        "signed in again (click Reconnect wallet); it sells at the market price on the first check after that",
                        signal.symbol)
        return self._finalize(Decision(id=did, action=Action.STAY_OUT, reason=(
            f"{tag} exit held: {livemod.SESSION_EXPIRED_REASON}. It sells on the first check after you sign in again"), **base))

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
                wallet_limits=self.wallet_settings.peek())).get("per_order_cap_usdt")
            upd: dict = {}
            if self.user_limits is not None:  # Trading limits panel: Auto's size cap follows the user's order size
                size_pct = limitsmod.engine_size_pct(self.user_limits)
                if size_pct != getattr(cfg, "max_size_pct", size_pct):
                    upd["max_size_pct"] = float(size_pct)
            if cap and cap != cfg.max_order_usdt:
                upd["max_order_usdt"] = float(cap)
            if upd:
                try:
                    import dataclasses
                    eng.live_config = dataclasses.replace(cfg, **upd)
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
        bal = (self.wcache["state"] or {}).get("balances") if self.live else None  # cached read: no baw call per tick
        if bal is None and not self.live:
            try:
                bal = livemod.read_exchange_wallet(ex)[1]
            except Exception:
                bal = None
        if bal is not None:
            ctx["equity"] = bal.get(livemod.QUOTE_ASSET, 0.0)
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
        """Today's LIVE BUY spend: settled fills (gross, fee included) + in-flight buys (quote notional)."""
        done = float((self.state.get("live_bought") or {}).get(utc_now().date().isoformat(), 0.0))
        return done + sum(p["notional"] for p in self.chain_pending.values())

    def _record_live_buy(self, res: ExecutionResult) -> None:
        """Settled LIVE buys today, gross: notional + fee (same as Bot 3's daily cap, 8773b0c). Only settled fills are
        added; in-flight buys are counted separately by daily_live_notional() and must not be baked in here."""
        day = utc_now().date().isoformat()
        done = float((self.state.get("live_bought") or {}).get(day, 0.0))
        self.state.update(live_bought={day: done + res.notional + (res.fee or 0.0)})

    def wallet(self) -> dict:
        """GET /api/wallet (Bot 1 shape). LIVE/PAPER: from the exchange (get_wallet/get_balances); SIM: simulated from
        the book. Never shows made-up balances in LIVE. limits = Bot 3's live_limits() merged over the app's."""
        with self.lock:
            pf = self.book.portfolio()
            address, bal, expired = None, None, False
            if not self.sim:
                st = self._cached_wallet_state() if self.live else livemod.read_exchange_wallet_state(self.c.exchange)
                address, bal, expired = st["address"], st["balances"], bool(st["session_expired"])
            address = address or self.state.get("wallet_address") or self.s.wallet_address
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
            wl = self.wallet_settings.peek() if self.live else None
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
                "chain_id": livemod.BSC_CHAIN_ID, "connected": connected and not expired, "session_expired": expired, "balances": rows,
                "bnb_gas_reserve": rl.get("bnb_gas_reserve", self.s.live_min_bnb), "limits": limits,
                "updated_at": (self.wcache["read_at"] if self.live else None) or utc_now(),
                **(self.wallet_cache_info() if self.live else {}),
            }

    # ------------------------------------------------------------------ Trading limits panel (/api/settings/limits)
    def _init_limits(self) -> None:
        """Risk owns the limits (Bot 3 set_live_limits); LOG_DIR/limits.json is applied on top at startup."""
        self.limits_store = limitsmod.LimitsStore(self.s.log_dir)
        self.limits_fallback = None if limitsmod.has_risk_limits(self.c.risk) else limitsmod.FallbackLimits(self.s, self.c.risk)
        self.user_limits: Optional[dict] = None  # current panel values once the user changed them (None = defaults)
        saved = self.limits_store.load()
        if saved:
            try:
                self._limits_call("set", saved, wallet_daily=None)
                self._after_limits_change("restored from limits.json")
            except ValueError as e:
                try:
                    self._limits_call("reset", wallet_daily=None)
                    limitsmod.apply_exchange(self.c.exchange, limitsmod.panel(self._limits_call("get", wallet_daily=None)))
                except Exception:
                    log.exception("could not restore the default limits")
                self._alert(AlertLevel.WARNING, "LIMITS", f"saved trading limits ignored ({e}); using the safe defaults")

    def _wallet_limits_for_risk(self) -> Any:
        """The real wallet's limits (LIVE only): the SIM client's simulated 100 USDT quota must not block a SIM demo."""
        if not self.live:
            return None
        try:
            return self.wallet_settings.peek()  # the cached read: the API never calls baw
        except Exception:
            return None

    def wallet_daily_limit(self) -> Optional[float]:
        v = getattr(self._wallet_limits_for_risk(), "daily_limit_usdt", None)
        return float(v) if v is not None else None

    def _limits_call(self, op: str, body: Optional[dict] = None, wallet_daily: Any = "live") -> dict:
        wl = self._wallet_limits_for_risk() if wallet_daily == "live" else None
        fb = self.limits_fallback
        if fb is not None:
            daily = getattr(wl, "daily_limit_usdt", None)
            return fb.settings(daily) if op == "get" else fb.reset(daily) if op == "reset" else fb.set(body or {}, daily)
        r = self.c.risk
        if op == "get":
            return dict(r.live_limits_settings(wl))
        if op == "reset":
            return dict(r.reset_live_limits(wl))
        return dict(r.set_live_limits(body or {}, wl))

    def limits_view(self) -> dict:
        with self.lock:
            out = self._limits_call("get")
            out["is_default"] = limitsmod.panel(out) == limitsmod.panel(out.get("defaults") or {})
            return out

    def set_limits(self, raw: dict) -> dict:
        """Validate (ValueError -> 422; nothing changes), apply to risk + engine + exchange, save, alert. Never touches
        pause or the emergency stop."""
        if not isinstance(raw, dict):
            raise limitsmod.LimitsError("Send the limits as a JSON object.")
        with self.lock:
            prev = limitsmod.panel(self._limits_call("get", wallet_daily=None))
            out = self._limits_call("set", {k: v for k, v in raw.items() if k not in limitsmod.READ_ONLY})
            try:  # the exchange can refuse too (Bot 4: BINANCE_LIVE_HARD_MAX_ORDER_USDT): then nothing changes
                limitsmod.apply_exchange(self.c.exchange, limitsmod.panel(out))
            except ValueError:
                self._limits_call("set", prev, wallet_daily=None)
                limitsmod.apply_exchange(self.c.exchange, prev)
                raise
            self._after_limits_change("changed from the dashboard", out)
            return {**self.limits_view(), "notes": list(out.get("notes") or [])}

    def reset_limits(self) -> dict:
        with self.lock:
            out = self._limits_call("reset")
            self._after_limits_change("reset to the safe defaults", out)
            return {**self.limits_view(), "notes": list(out.get("notes") or [])}

    def _after_limits_change(self, why: str, out: Optional[dict] = None) -> None:
        cur = self._limits_call("get", wallet_daily=None)
        d = limitsmod.panel(cur)
        is_default = d == limitsmod.panel(cur.get("defaults") or {})
        limitsmod.apply_settings(self.s, d)
        applied = [limitsmod.apply_engine(self.c.engine, d), limitsmod.apply_exchange(self.c.exchange, d)]
        self.user_limits = None if is_default else d
        self.limits_store.save(None if is_default else d)
        self._sync_explainer_limits()
        notes = "; ".join((out or {}).get("notes") or [])
        self._alert(AlertLevel.INFO, "LIMITS_CHANGED", f"Trading limits {why}: {limitsmod.describe(d)}" + (f" ({notes})" if notes else ""))
        log.info("limits %s: %s (also applied: %s)", why, d, ", ".join(applied))

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
                ctx = livemod.make_live_context(wallet_limits=self.wallet_settings.peek() if self.live else None,
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
        connected, expired = False, False
        info: dict = {}
        if self.live:  # the loop's LAST wallet read (never calls baw); a failed read is never shown as connected
            st = self._cached_wallet_state()
            connected, expired = bool(st["connected"]), bool(st["session_expired"])
            info = self.wallet_cache_info()
        reasons: list[str] = []
        if not self.live:
            reasons.append(f"TRADING_MODE is {self.s.trading_mode.value}, not LIVE")
        if not self.s.live_trading_enabled:
            reasons.append("LIVE_TRADING_ENABLED is not true")
        if not self.s.binance_live_enabled:
            reasons.append("BINANCE_LIVE_ENABLED is not true")
        if not allow:
            reasons.append("risk allow_live is off (RISK_ALLOW_LIVE=true)")
        if self.live and expired:
            reasons.append(livemod.SESSION_EXPIRED_REASON)
        elif self.live and info.get("stale"):
            nxt = info.get("next_retry_at")
            reasons.append("wallet read failed (network); no BUYs until a fresh read succeeds"
                           + (f" (next try {nxt:%H:%M:%S} UTC)" if nxt else ""))
        elif self.live and info.get("read_at") is None:
            reasons.append("wallet not read yet")
        elif self.live and not connected:
            reasons.append("wallet not connected (no balances from the exchange client)")
        if self.wallet_block:
            reasons.append("different wallet connected")
        ctrl = self.control()
        if ctrl.emergency_stopped:
            reasons.append(one_prefix("Emergency stop", ctrl.reason, "active"))
        if ctrl.paused and not (expired and (ctrl.reason or "") in SESSION_PAUSE_REASONS):  # no duplicate session line
            reasons.append(one_prefix("Paused", ctrl.reason, "by user"))
        return {"wallet_block": self.wallet_block, "session_expired": expired, "live_trading_enabled": self.s.live_trading_enabled, "binance_live_enabled": self.s.binance_live_enabled,
                "risk_allow_live": allow, "wallet_connected": connected, "can_trade": not reasons, "blocked_reasons": reasons,
                "wallet_read_at": info.get("read_at"), "wallet_age_s": info.get("age_s"), "wallet_stale": bool(info.get("stale")),
                "cooldowns": self.active_cooldowns(),
                "chain_pending": [{"order_id": v["res"].order_id, "client_order_id": k, "symbol": v["symbol"],
                                   "notional": v["notional"], "tx_hash": v["res"].tx_hash, "since": v["since"].isoformat(),
                                   "held_exit": v.get("held_exit")} for k, v in self.chain_pending.items()]}

    # ------------------------------------------------------------------ dashboard wallet sign-in (v0.8)
    def expected_wallet_address(self) -> Optional[str]:
        """The wallet the saved state belongs to: agent_state.<mode>.json wallet_address, else BINANCE_WALLET_ADDRESS."""
        return self.state.get("wallet_address") or self.s.wallet_address or None

    def refresh_wallet(self) -> None:
        """Drop every cached wallet read (balances, wallet settings/limits, token statuses) and re-read them now."""
        ex = self.c.exchange
        for obj in (ex, getattr(ex, "executor", None), getattr(ex, "_executor", None)):
            if obj is not None and hasattr(obj, "_wallet_cache"):
                obj._wallet_cache = None
        self._token_status_cache.clear()
        self.wallet_settings.invalidate()
        self._order_memo = None
        self._warning_codes = set()

    def on_wallet_connected(self, address: Optional[str], changed: bool) -> None:
        """After a dashboard sign-in: re-read the wallet + limits; a different wallet blocks all orders until reconciled."""
        with self.lock:
            self.refresh_wallet()
            if self.live:  # one fresh read (incl. settings) right after the sign-in; clears an expired session + its pause
                self._wallet_read(user=True)
            if changed:
                self.wallet_block = {"address": address, "expected": self.expected_wallet_address(), "since": utc_now().isoformat()}
                self.state.update(wallet_block=self.wallet_block)
                self._alert(AlertLevel.CRITICAL, "WALLET_CHANGED", f"different wallet connected ({address}); no orders until the "
                            "saved positions are checked against it (POST /api/wallet/reconcile)")
            elif address:
                self.state.update(wallet_address=address)
                self._alert(AlertLevel.INFO, "WALLET_CONNECTED", f"wallet {address} connected; balances and limits re-read")
            self.bus.publish(EventType.STATUS, self.status())

    def reconcile_wallet(self) -> dict:
        """POST /api/wallet/reconcile: check saved open lots against the connected wallet's balances (oldest lot first).
        Lots the wallet doesn't (fully) hold are dropped from the book (no trade); then the wallet becomes the saved
        wallet and the 'different wallet connected' block is cleared. SIM: the wallet is the book, so nothing drops."""
        with self.lock:
            if self.chain_pending:
                raise AgentStateError("orders are still settling on chain; reconcile after they confirm")
            self.refresh_wallet()
            address, bal = None, None
            if self.live:  # one fresh read (incl. settings) for the reconcile
                before = self.wcache["read_at"]
                st = self._wallet_read(user=True)
                if self.wcache["stale"] or self.wcache["read_at"] == before:
                    raise AgentStateError(f"wallet read failed ({self.wcache['error'] or 'network error'}); cannot reconcile")
                address, bal = st["address"], st["balances"]
                if bal is None:
                    raise AgentStateError("wallet balances unavailable; cannot reconcile")
            elif not self.sim:
                try:
                    address, bal, _ = livemod.read_exchange_wallet(self.c.exchange)
                except Exception as e:
                    raise AgentStateError(f"wallet balances unavailable ({type(e).__name__}: {e}); cannot reconcile")
                if bal is None:
                    raise AgentStateError("wallet balances unavailable; cannot reconcile")
            else:
                bal = {}
                for l in self.book.lots:
                    bal[l.position.symbol] = bal.get(l.position.symbol, 0.0) + l.position.quantity
            address = address or (self.wallet_block or {}).get("address") or self.expected_wallet_address()
            kept, dropped, left = [], [], dict(bal)
            for lot in sorted(self.book.lots, key=lambda l: l.position.opened_at):
                p = lot.position
                row = {"position_id": p.id, "symbol": p.symbol, "quantity": p.quantity, "held": round(bal.get(p.symbol, 0.0), 10)}
                have = left.get(p.symbol, 0.0)
                if have + 1e-9 + 1e-6 * p.quantity >= p.quantity:
                    left[p.symbol] = have - p.quantity
                    kept.append(row)
                else:
                    self.book.lots.remove(lot)
                    dropped.append({**row, "reason": "not held by the connected wallet"})
            was = self.wallet_block
            self.wallet_block = None
            self.state.update(wallet_block=None, **({"wallet_address": address} if address else {}))
            self._persist()
            self._alert(AlertLevel.WARNING if dropped else AlertLevel.INFO, "WALLET_RECONCILED",
                        f"wallet {address}: {len(kept)} saved position(s) kept, {len(dropped)} dropped (not in the wallet)"
                        + ("; live trading unblocked" if was else ""))
            self.bus.publish(EventType.STATUS, self.status())
            return {"address": address, "was_blocked": bool(was), "kept": kept, "dropped": dropped, "wallet_blocked": False}

    def _add_awaiting(self, obj: Any) -> Any:
        if isinstance(obj, list):
            return [self._add_awaiting(v) for v in obj]
        if isinstance(obj, dict):
            for k in ("last_decision", "recent_decisions"):
                if k in obj:
                    obj[k] = self._add_awaiting(obj[k])
            if "action" in obj and obj.get("id") in self._awaiting:
                obj["awaiting_decision_id"] = self._awaiting[obj["id"]]
        return obj

    def enrich_payload(self, payload: Any) -> Any:
        out = self._add_awaiting(livemod.enrich_payload(payload, self.exec_meta.get))
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
        self.wallet_block = st.get("wallet_block") or None  # survives restarts until reconciled
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
        tag = reason.value if reason else "EXIT"
        if self.live:  # fresh session state before the sell; a network failure (stale read) still lets the exit go out
            self._order_wallet_read(with_settings=False)
        if self.live and self.wallet_session["expired"]:
            return self._held_exit_session(signal, tag, base, did)
        last: Optional[ExecutionResult] = None
        filled = 0
        for lot in list(lots):
            q = lot.position.quantity
            px = signal.price or self.book.prices.get(sym) or lot.position.avg_entry_price
            res = last = self._execute(OrderSide.SELL, sym, q * px, did, quantity=q, fill_price=signal.price)
            if self.live and not (res.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED) and res.filled_quantity > 0) \
                    and (livemod.result_reason(res) in livemod.SESSION_REASONS or livemod.is_session_expired(res.error)):
                self._session_expired(res.error or "session_expired")
                if not filled:  # nothing sold: one plain held decision, no more swaps until the user signs in again
                    return self._held_exit_session(signal, tag, base, did)
                break
            if res.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED) and res.filled_quantity > 0:
                filled += 1
                self.book.close_lot(lot, res, reason.value if reason else None, self._now())
                self.perf.record_trade(self.book.closed[-1].pnl)
                limit = self.book.starting_capital * DAILY_LOSS_LIMIT_PCT / 100
                self._publish_alerts(self.alerts.on_trade_closed(self.perf.consecutive_losses, self.book.daily_pnl, limit, sym))
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
