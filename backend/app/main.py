"""FastAPI app: REST + WebSocket for the UI, wiring all modules (see API.md).

Run: uvicorn backend.app.main:app --port 8000      (or backend/app/run.sh)
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware

from backend.contracts import (
    ActivationMode, utc_now, CONTRACTS_VERSION, Alert, AlertLevel, AutoConfig, BacktestRequest, BacktestResult, Candle, ControlState, Decision, Event, EventType, ManualStrategy,
    LoopState, PerformanceSummary, Portfolio, RegimeAnalysis, SystemInfo, StatusSnapshot, StrategySelection, Timeframe, TokenInfo, TraderStyle, PresetName,
)
from backend.monitoring import EventLog

from .events import EventBus
from .loop import AgentStateError, TradingLoop
from .schemas import (
    AskRequest, ConfirmationModeRequest, EmergencyStopRequest, LoopStartRequest, ManualActivateRequest, ParseRequest,
    ParseResponse, PresetsTable, PresetsTableEntry, ReasonRequest, DecisionView, Heartbeat, LiveStatus, LoopStateView,
    StatusView, SystemInfoView, WalletInfo,
)
from .live import agent_identity
from .settings import Settings
from .store import StrategyStore
from .wiring import Components, build_components, presets_table

log = logging.getLogger("backend.app")


class AppState:
    def __init__(self, settings: Settings, comps: Components) -> None:
        self.settings = settings
        self.comps = comps
        path = os.path.join(settings.log_dir, "events.jsonl") if settings.log_dir else None
        self.bus = EventBus(EventLog(path))
        self.loop = TradingLoop(settings, comps, self.bus)
        self.store = StrategyStore(settings.log_dir)
        self.interval = settings.loop_interval_seconds
        self.task: Optional[asyncio.Task] = None
        self.want_running = False  # watchdog restarts the loop task while this is True
        self.monitor: Optional[asyncio.Task] = None
        self.stall_alerted = False


def create_app(settings: Optional[Settings] = None, components: Optional[Components] = None) -> FastAPI:
    settings = settings or Settings.from_env()
    comps = components or build_components(settings)
    st = AppState(settings, comps)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        st.aio = asyncio.get_running_loop()
        st.monitor = asyncio.create_task(_monitor(st))
        if settings.loop_autostart:
            await run_in_threadpool(_autostart, st)
        yield
        st.monitor.cancel()
        await _stop_task(st)

    app = FastAPI(title="Autonomous On-Chain Stock Trading Agent", version=CONTRACTS_VERSION, lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
    app.state.st = st
    L = st.loop

    def guard(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except AgentStateError as e:
            raise HTTPException(409, str(e))
        except KeyError as e:
            raise HTTPException(404, f"not found: {e.args[0] if e.args else ''}")

    # ---------------------------------------------------------------- system
    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "contracts_version": CONTRACTS_VERSION, "trading_mode": settings.trading_mode.value}

    def dv(d: Decision) -> DecisionView:  # + LIVE execution fields (explorer_url, route, quote_expires_at, real_funds)
        return DecisionView.model_validate(L.enrich_payload(d.model_dump(mode="json")))

    @app.get("/api/system", response_model=SystemInfoView)
    def system() -> SystemInfoView:
        return SystemInfoView(trading_mode=settings.trading_mode, live_enabled=settings.live_enabled, contracts_version=CONTRACTS_VERSION,
                              sim_seed=settings.sim_seed if L.sim else None, components=comps.names(),
                              live=LiveStatus.model_validate(L.live_status()), agent_identity=agent_identity())

    @app.get("/api/live/status", response_model=LiveStatus)
    def live_status() -> LiveStatus:
        return LiveStatus.model_validate(L.live_status())

    @app.get("/api/wallet", response_model=WalletInfo)
    def wallet() -> WalletInfo:
        return WalletInfo.model_validate(L.wallet())

    # ---------------------------------------------------------------- market data
    @app.get("/api/tokens", response_model=list[TokenInfo])
    def tokens(q: str = "") -> list[TokenInfo]:
        toks = comps.exchange.search_tokens(q)
        if not L.sim:
            return toks
        return [t.model_copy(update={"price": guard(L.view_price, t.symbol)}) for t in toks]

    @app.get("/api/candles/{symbol}", response_model=list[Candle])
    def candles(symbol: str, timeframe: Timeframe = Timeframe.H1, limit: Optional[int] = None, history: bool = False) -> list[Candle]:
        """Default: replay-pinned (SIM: never past the loop's bar), last `limit` (200). history=true: full history
        (SIM: the cached seeded series, same bars /api/backtest uses; PAPER/LIVE: the exchange's candles), last
        `limit` if given."""
        if history:
            return guard(L.history_candles, symbol, timeframe, limit)
        return guard(L.view_candles, symbol, timeframe, 200 if limit is None else limit)

    @app.get("/api/price/{symbol}")
    def price(symbol: str) -> dict:
        return {"symbol": symbol, "price": guard(L.view_price, symbol)}

    @app.get("/api/regime/{symbol}", response_model=RegimeAnalysis)
    def regime(symbol: str, timeframe: Timeframe = Timeframe.H1) -> RegimeAnalysis:
        return comps.engine.detect_regime(guard(L.view_candles, symbol, timeframe, settings.window_bars))

    @app.get("/api/auto/selection", response_model=StrategySelection)
    def selection(symbol: str, style: TraderStyle = TraderStyle.BALANCED, timeframe: Timeframe = Timeframe.H1) -> StrategySelection:
        ra = regime(symbol, timeframe)
        return comps.engine.select_strategy(ra.regime, style, list(PresetName))

    @app.get("/api/auto/presets-table", response_model=PresetsTable)
    def auto_presets_table() -> PresetsTable:
        source, rows = presets_table(comps.engine)
        return PresetsTable(source=source, entries=[PresetsTableEntry(regime=r.regime, style=r.style, preset=r.preset,
                                                                      size_multiplier=r.size_multiplier, reason=r.reason) for r in rows])

    # ---------------------------------------------------------------- strategies
    @app.get("/api/strategies", response_model=list[ManualStrategy])
    def list_strategies() -> list[ManualStrategy]:
        return st.store.list()

    @app.post("/api/strategies", response_model=ManualStrategy)
    def save_strategy(s: ManualStrategy) -> ManualStrategy:
        return st.store.save(s)

    @app.post("/api/strategies/validate", response_model=ManualStrategy)
    def validate_strategy(s: ManualStrategy) -> ManualStrategy:
        return s

    @app.get("/api/strategies/{sid}", response_model=ManualStrategy)
    def get_strategy(sid: str) -> ManualStrategy:
        return guard(st.store.get, sid)

    @app.put("/api/strategies/{sid}", response_model=ManualStrategy)
    def put_strategy(sid: str, s: ManualStrategy) -> ManualStrategy:
        return guard(st.store.update, sid, s)

    @app.delete("/api/strategies/{sid}")
    def delete_strategy(sid: str) -> dict:
        guard(st.store.delete, sid)
        return {"deleted": sid}

    @app.post("/api/backtest", response_model=BacktestResult)
    def backtest(req: BacktestRequest) -> BacktestResult:
        c = guard(L.history_candles, req.strategy.symbol, req.strategy.timeframe, req.limit)  # never replay-pinned
        return (comps.backtest_engine or comps.engine).backtest_manual(req.strategy, c, req.initial_capital)

    # ---------------------------------------------------------------- agent & loop
    @app.post("/api/manual/activate", response_model=StatusSnapshot)
    def manual_activate(req: ManualActivateRequest) -> StatusSnapshot:
        if req.strategy is not None:
            s = req.strategy
        elif req.strategy_id:
            s = guard(st.store.get, req.strategy_id)
        else:
            raise HTTPException(422, "strategy_id or strategy required")
        return guard(L.activate_manual, s)

    @app.post("/api/auto/activate", response_model=StatusSnapshot)
    def auto_activate(cfg: AutoConfig) -> StatusSnapshot:
        return guard(L.activate_auto, cfg)

    @app.post("/api/agent/deactivate", response_model=StatusSnapshot)
    async def deactivate() -> StatusSnapshot:
        await _stop_task(st)
        return await run_in_threadpool(L.deactivate)

    def loop_state() -> LoopStateView:
        return LoopStateView(running=st.task is not None and not st.task.done(), interval_seconds=st.interval, ticks=L.ticks,
                             agent_mode=L.agent_mode, active_symbol=L.symbol, active_strategy=L.active_strategy, replay_finished=L.finished,
                             heartbeat=Heartbeat(**L.heartbeat(), autostart=settings.loop_autostart))

    @app.get("/api/loop", response_model=LoopStateView)
    def get_loop() -> LoopStateView:
        return loop_state()

    @app.post("/api/loop/tick", response_model=list[DecisionView])
    def tick() -> list[DecisionView]:
        return [dv(d) for d in guard(L.tick)]

    @app.post("/api/loop/start", response_model=LoopStateView)
    async def start(req: Optional[LoopStartRequest] = None) -> LoopState:
        if L.symbol is None:
            raise HTTPException(409, "no active agent: activate a manual strategy or auto config first")
        if req and req.interval_seconds:
            st.interval = req.interval_seconds
        _start_task(st)
        return loop_state()

    @app.post("/api/loop/stop", response_model=LoopStateView)
    async def stop() -> LoopState:
        await _stop_task(st)
        return loop_state()

    # ---------------------------------------------------------------- decisions & confirmations
    @app.get("/api/status", response_model=StatusView)
    def status() -> StatusView:
        return StatusView.model_validate(L.enrich_payload(L.status().model_dump(mode="json")))

    @app.get("/api/portfolio", response_model=Portfolio)
    def portfolio() -> Portfolio:
        with L.lock:
            return L.book.portfolio()

    @app.get("/api/decisions", response_model=list[DecisionView])
    def decisions(limit: int = 50, symbol: Optional[str] = None) -> list[DecisionView]:
        with L.lock:
            ds = [d for d in L.decisions if symbol is None or d.symbol == symbol]
        return [dv(d) for d in ds[::-1][:limit]]

    @app.get("/api/decisions/{did}", response_model=DecisionView)
    def decision(did: str) -> DecisionView:
        with L.lock:
            for d in L.decisions:
                if d.id == did:
                    return dv(d)
        raise HTTPException(404, f"not found: {did}")

    @app.get("/api/confirmations", response_model=list[DecisionView])
    def confirmations() -> list[DecisionView]:
        return [dv(d) for d in L.pending_decisions()]

    @app.post("/api/confirmations/{did}/approve", response_model=DecisionView)
    def approve(did: str) -> DecisionView:
        return dv(guard(L.approve, did))

    @app.post("/api/confirmations/{did}/reject", response_model=DecisionView)
    def reject(did: str, req: Optional[ReasonRequest] = None) -> DecisionView:
        return dv(guard(L.reject, did, req.reason if req else ""))

    @app.post("/api/positions/by-id/{position_id}/close", response_model=DecisionView)
    def close_position_by_id(position_id: str) -> DecisionView:
        return dv(guard(L.close_position, position_id))

    @app.post("/api/positions/{symbol}/close", response_model=DecisionView)
    def close_position(symbol: str) -> DecisionView:
        return dv(guard(L.close_symbol, symbol))

    # ---------------------------------------------------------------- control
    @app.get("/api/control", response_model=ControlState)
    def control() -> ControlState:
        return L.control()

    @app.post("/api/control/pause", response_model=ControlState)
    def pause(req: Optional[ReasonRequest] = None) -> ControlState:
        return L.pause(req.reason if req else "")

    @app.post("/api/control/resume", response_model=ControlState)
    def resume() -> ControlState:
        return L.resume()

    @app.post("/api/control/reset-emergency-stop", response_model=ControlState)
    def reset_emergency_stop() -> ControlState:
        return L.reset_emergency_stop()

    @app.post("/api/control/emergency-stop", response_model=ControlState)
    def emergency_stop(req: Optional[EmergencyStopRequest] = None) -> ControlState:
        req = req or EmergencyStopRequest()
        return L.emergency_stop(req.reason, req.flatten)

    @app.post("/api/control/confirmation-mode", response_model=ControlState)
    def confirmation_mode(req: ConfirmationModeRequest) -> ControlState:
        return L.set_confirmation_mode(req.enabled)

    # ---------------------------------------------------------------- monitoring
    @app.get("/api/alerts", response_model=list[Alert])
    def alerts(limit: int = 50) -> list[Alert]:
        return L.alerts.alerts(limit)

    @app.get("/api/performance", response_model=PerformanceSummary)
    def performance() -> PerformanceSummary:
        with L.lock:
            return L.perf.summary(L.book.portfolio())

    # ---------------------------------------------------------------- LLM (never trades)
    @app.post("/api/llm/parse", response_model=ParseResponse)
    def llm_parse(req: ParseRequest) -> ParseResponse:
        r = comps.explainer.parse_strategy_from_text(req.text, req.default_symbol)
        if isinstance(r, AutoConfig):
            return ParseResponse(kind="AUTO", config=r)
        return ParseResponse(kind="MANUAL", strategy=ManualStrategy.model_validate(r.model_dump()))

    @app.post("/api/llm/ask")
    def llm_ask(req: AskRequest) -> dict:
        return {"answer": L.answer(req.question)}

    @app.get("/api/llm/summary")
    def llm_summary() -> dict:
        return {"summary": L.summarize()}

    # ---------------------------------------------------------------- websocket
    @app.websocket("/ws")
    async def ws(socket: WebSocket) -> None:
        await socket.accept()
        q: asyncio.Queue[Event] = asyncio.Queue()
        sid = st.bus.subscribe_async(asyncio.get_running_loop(), q)

        async def send_status() -> None:
            snap = await run_in_threadpool(L.status)
            await socket.send_text(Event(type=EventType.STATUS, payload=snap.model_dump(mode="json")).model_dump_json())

        async def pump() -> None:
            while True:
                ev = await q.get()
                await socket.send_text(ev.model_dump_json())

        try:
            await send_status()
            sender = asyncio.create_task(pump())
            try:
                while True:
                    msg = await socket.receive_json()
                    if isinstance(msg, dict) and msg.get("type") == "ping":
                        await send_status()
            finally:
                sender.cancel()
        except WebSocketDisconnect:
            pass
        finally:
            st.bus.unsubscribe(sid)

    return app


async def _run(st: AppState) -> None:
    """One loop task. A failing tick is an alert, not a crash; repeated failures back off (max 12 intervals)."""
    fails = 0
    while True:
        try:
            await run_in_threadpool(st.loop.tick)
            fails = 0
        except AgentStateError:
            st.want_running = False
            return
        except Exception as e:  # keep the loop alive; surface it as an alert
            fails += 1
            st.loop.last_error = f"{type(e).__name__}: {e}"
            log.exception("tick failed")
            st.loop._alert(AlertLevel.WARNING, "TICK_FAILED", f"a loop cycle failed ({type(e).__name__}); retrying with backoff")
        if st.loop.finished:
            st.want_running = False
            return
        await asyncio.sleep(st.interval * min(2 ** fails, 12) if fails else st.interval)


async def _supervise(st: AppState) -> None:
    """Watchdog: restarts the loop task if it crashes, with exponential backoff (1s .. 60s)."""
    backoff = 1.0
    while st.want_running:
        try:
            await _run(st)
            return
        except asyncio.CancelledError:
            raise
        except BaseException as e:  # noqa: BLE001 - anything that escaped _run
            st.loop.restarts += 1
            st.loop.last_error = f"{type(e).__name__}: {e}"
            log.exception("loop task crashed; restarting in %.0fs", backoff)
            st.loop._alert(AlertLevel.CRITICAL, "LOOP_RESTARTED", f"loop crashed ({type(e).__name__}); restart #{st.loop.restarts} in {backoff:.0f}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


def _start_task(st: AppState) -> None:
    st.want_running = True
    if st.task is None or st.task.done():
        st.task = asyncio.create_task(_supervise(st))


async def _monitor(st: AppState) -> None:
    """Stall alert: no tick for `stall_intervals` intervals while the loop should run. Also restarts a dead task."""
    while True:
        await asyncio.sleep(max(st.interval, 0.05))
        try:
            check_stall(st)
            if st.want_running and (st.task is None or st.task.done()) and not st.loop.finished and st.loop.symbol:
                st.loop.restarts += 1
                st.loop._alert(AlertLevel.WARNING, "LOOP_RESTARTED", "loop task was not running; restarted by the watchdog")
                _start_task(st)
        except Exception:  # pragma: no cover - the monitor must never die
            log.exception("monitor failed")


def check_stall(st: AppState) -> bool:
    if not st.want_running:
        st.stall_alerted = False
        return False
    last = st.loop.last_tick_at or st.loop.started_at
    limit = st.settings.stall_intervals * st.interval
    stalled = (utc_now() - last).total_seconds() > limit
    if stalled and not st.stall_alerted:
        st.loop._alert(AlertLevel.CRITICAL, "LOOP_STALLED", f"no tick for more than {limit:.0f}s ({st.settings.stall_intervals:g} intervals)")
    st.stall_alerted = stalled
    return stalled


def _autostart(st: AppState) -> None:
    """LOOP_AUTOSTART=true: resume the saved agent (restored by the loop) or start the default demo agent
    (Auto, DEFAULT_AGENT_SYMBOL; PAPER/LIVE default NVDAB), then start the loop. Never overrides an emergency stop
    (the loop runs; exits/flatten work, BUYs stay blocked until reset)."""
    L = st.loop
    if L.symbol is None:
        sym = st.settings.default_agent_symbol or ("NVDAB" if not L.sim else None)
        if sym:
            try:
                L.activate_auto(AutoConfig(symbol=sym, capital=L.book.starting_capital, activation=ActivationMode.AUTO_EXECUTE))
            except Exception as e:
                L._alert(AlertLevel.WARNING, "AUTOSTART", f"could not start the default agent on {sym}: {e}")
    if L.symbol is None:
        L._alert(AlertLevel.WARNING, "AUTOSTART", "LOOP_AUTOSTART is on but no agent is saved or configured; loop not started")
        return
    asyncio.run_coroutine_threadsafe(_astart(st), st.aio).result(timeout=10)


async def _astart(st: AppState) -> None:
    _start_task(st)


async def _stop_task(st: AppState) -> None:
    st.want_running = False
    t, st.task = st.task, None
    if t and not t.done():
        t.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await t


app = create_app()
