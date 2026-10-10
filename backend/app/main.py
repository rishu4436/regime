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
    CONTRACTS_VERSION, AutoConfig, BacktestResult, Candle, ControlState, Decision, Event, EventType, ManualStrategy,
    Portfolio, RegimeAnalysis, StatusSnapshot, StrategySelection, Timeframe, TokenInfo, TraderStyle, PresetName,
)
from backend.monitoring import Alert, AlertLevel, EventLog, PerformanceSummary

from .events import EventBus
from .loop import AgentStateError, TradingLoop
from .schemas import (
    AskRequest, BacktestRequest, ConfirmationModeRequest, EmergencyStopRequest, LoopStartRequest, LoopState,
    ManualActivateRequest, ParseRequest, ParseResponse, ReasonRequest, SystemInfo,
)
from .settings import Settings
from .store import StrategyStore
from .wiring import Components, build_components

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


def create_app(settings: Optional[Settings] = None, components: Optional[Components] = None) -> FastAPI:
    settings = settings or Settings.from_env()
    comps = components or build_components(settings)
    st = AppState(settings, comps)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
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

    @app.get("/api/system", response_model=SystemInfo)
    def system() -> SystemInfo:
        return SystemInfo(trading_mode=settings.trading_mode, live_enabled=settings.live_enabled, contracts_version=CONTRACTS_VERSION,
                          sim_seed=settings.sim_seed if L.sim else None, components=comps.names())

    # ---------------------------------------------------------------- market data
    @app.get("/api/tokens", response_model=list[TokenInfo])
    def tokens(q: str = "") -> list[TokenInfo]:
        return comps.exchange.search_tokens(q)

    @app.get("/api/candles/{symbol}", response_model=list[Candle])
    def candles(symbol: str, timeframe: Timeframe = Timeframe.H1, limit: int = 200) -> list[Candle]:
        return guard(comps.exchange.get_candles, symbol, timeframe, limit)

    @app.get("/api/price/{symbol}")
    def price(symbol: str) -> dict:
        return {"symbol": symbol, "price": guard(comps.exchange.get_price, symbol)}

    @app.get("/api/regime/{symbol}", response_model=RegimeAnalysis)
    def regime(symbol: str, timeframe: Timeframe = Timeframe.H1) -> RegimeAnalysis:
        return comps.engine.detect_regime(guard(comps.exchange.get_candles, symbol, timeframe, settings.window_bars))

    @app.get("/api/auto/selection", response_model=StrategySelection)
    def selection(symbol: str, style: TraderStyle = TraderStyle.BALANCED, timeframe: Timeframe = Timeframe.H1) -> StrategySelection:
        ra = regime(symbol, timeframe)
        return comps.engine.select_strategy(ra.regime, style, list(PresetName))

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
        c = guard(comps.exchange.get_candles, req.strategy.symbol, req.strategy.timeframe, req.limit)
        return comps.engine.backtest_manual(req.strategy, c, req.initial_capital)

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

    def loop_state() -> LoopState:
        return LoopState(running=st.task is not None and not st.task.done(), interval_seconds=st.interval, ticks=L.ticks,
                         agent_mode=L.agent_mode, active_symbol=L.symbol, active_strategy=L.active_strategy, replay_finished=L.finished)

    @app.get("/api/loop", response_model=LoopState)
    def get_loop() -> LoopState:
        return loop_state()

    @app.post("/api/loop/tick", response_model=list[Decision])
    def tick() -> list[Decision]:
        return guard(L.tick)

    @app.post("/api/loop/start", response_model=LoopState)
    async def start(req: Optional[LoopStartRequest] = None) -> LoopState:
        if L.symbol is None:
            raise HTTPException(409, "no active agent: activate a manual strategy or auto config first")
        if req and req.interval_seconds:
            st.interval = req.interval_seconds
        if st.task is None or st.task.done():
            st.task = asyncio.create_task(_run(st))
        return loop_state()

    @app.post("/api/loop/stop", response_model=LoopState)
    async def stop() -> LoopState:
        await _stop_task(st)
        return loop_state()

    # ---------------------------------------------------------------- decisions & confirmations
    @app.get("/api/status", response_model=StatusSnapshot)
    def status() -> StatusSnapshot:
        return L.status()

    @app.get("/api/portfolio", response_model=Portfolio)
    def portfolio() -> Portfolio:
        with L.lock:
            return L.book.portfolio()

    @app.get("/api/decisions", response_model=list[Decision])
    def decisions(limit: int = 50, symbol: Optional[str] = None) -> list[Decision]:
        with L.lock:
            ds = [d for d in L.decisions if symbol is None or d.symbol == symbol]
        return ds[::-1][:limit]

    @app.get("/api/decisions/{did}", response_model=Decision)
    def decision(did: str) -> Decision:
        with L.lock:
            for d in L.decisions:
                if d.id == did:
                    return d
        raise HTTPException(404, f"not found: {did}")

    @app.get("/api/confirmations", response_model=list[Decision])
    def confirmations() -> list[Decision]:
        return L.pending_decisions()

    @app.post("/api/confirmations/{did}/approve", response_model=Decision)
    def approve(did: str) -> Decision:
        return guard(L.approve, did)

    @app.post("/api/confirmations/{did}/reject", response_model=Decision)
    def reject(did: str, req: Optional[ReasonRequest] = None) -> Decision:
        return guard(L.reject, did, req.reason if req else "")

    @app.post("/api/positions/{symbol}/close", response_model=Decision)
    def close_position(symbol: str) -> Decision:
        return guard(L.close_symbol, symbol)

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
        return {"answer": comps.explainer.answer(req.question, L.status())}

    @app.get("/api/llm/summary")
    def llm_summary() -> dict:
        return {"summary": comps.explainer.summarize_state(L.status())}

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
    while True:
        try:
            await run_in_threadpool(st.loop.tick)
        except AgentStateError:
            return
        except Exception:  # keep the loop alive; surface it as an alert
            log.exception("tick failed")
            st.loop._alert(AlertLevel.WARNING, "TICK_FAILED", "a loop cycle failed; see server log")
        if st.loop.finished:
            return
        await asyncio.sleep(st.interval)


async def _stop_task(st: AppState) -> None:
    t, st.task = st.task, None
    if t and not t.done():
        t.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await t


app = create_app()
