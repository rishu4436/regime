"""Builds the four components strictly against the contract Protocols.

Real implementations come from the other bots' modules (same class names the conformance test uses).
While a module is still a stub (raises NotImplementedError) the app falls back to `fallbacks.py`:
- StrategyEngine / Explainer are stateless -> per-call fallback (each method switches over as soon as it is implemented).
- RiskManager is stateful -> chosen as a whole by probing `check()` once at startup.
- ExchangeClient comes from Bot 4's factory; its mode must equal TRADING_MODE (SIM/PAPER/LIVE separation).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from backend.contracts import (
    utc_now,
    Action, AgentMode, ControlState, Decision, ExchangeClient, Explainer, Portfolio, RiskManager, Signal, StrategyEngine,
    StrategySelection,
    TradingMode,
)

from .fallbacks import FallbackExplainer, FallbackRiskManager, FallbackStrategyEngine
from .settings import Settings

log = logging.getLogger("backend.app.wiring")


class _PerCallFallback:
    """Delegates to `primary`; on NotImplementedError (stub) - or any error if `catch_all` - uses `fallback`."""

    def __init__(self, primary: Any, fallback: Any, catch_all: bool = False) -> None:
        self._primary, self._fallback, self._catch_all = primary, fallback, catch_all
        self.fallback_methods: set[str] = set()

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        p = getattr(self._primary, name) if self._primary is not None else None
        f = getattr(self._fallback, name)
        if not callable(f):
            return p if p is not None else f

        def call(*a: Any, **kw: Any) -> Any:
            if p is None:
                self.fallback_methods.add(name)
                return f(*a, **kw)
            try:
                return p(*a, **kw)
            except NotImplementedError:
                self.fallback_methods.add(name)
                return f(*a, **kw)
            except Exception:
                if not self._catch_all:
                    raise
                log.exception("%s.%s failed, using fallback", type(self._primary).__name__, name)
                self.fallback_methods.add(name)
                return f(*a, **kw)

        return call

    def describe(self) -> str:
        if self._primary is None:
            return f"fallback:{type(self._fallback).__name__}"
        name = type(self._primary).__name__
        return name + (f" (fallback for: {', '.join(sorted(self.fallback_methods))})" if self.fallback_methods else "")


class ResilientEngine(_PerCallFallback):
    def detect_regime(self, candles): return self.__getattr__("detect_regime")(candles)
    def select_strategy(self, regime, style, enabled_presets): return self.__getattr__("select_strategy")(regime, style, enabled_presets)
    def evaluate_manual(self, strategy, candles, portfolio): return self.__getattr__("evaluate_manual")(strategy, candles, portfolio)
    def evaluate_auto(self, config, candles, portfolio): return self.__getattr__("evaluate_auto")(config, candles, portfolio)
    def backtest_manual(self, strategy, candles, initial_capital): return self.__getattr__("backtest_manual")(strategy, candles, initial_capital)


def supported_kwargs(fn: Any, kw: dict[str, Any]) -> dict[str, Any]:
    """Subset of `kw` that `fn` accepts (by name, or all if it takes **kwargs). Lets the app pass newer optional
    keyword args (e.g. Bot 5's `performance=` / `alerts=`) without breaking implementations that lack them."""
    import inspect

    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return {}
    if any(p.kind == p.VAR_KEYWORD for p in params.values()):
        return dict(kw)
    return {k: v for k, v in kw.items() if k in params and params[k].kind in (params[k].KEYWORD_ONLY, params[k].POSITIONAL_OR_KEYWORD)}


def call_with_supported(fn: Any, *args: Any, **kw: Any) -> Any:
    return fn(*args, **supported_kwargs(fn, {k: v for k, v in kw.items() if v is not None}))


class ResilientExplainer(_PerCallFallback):
    """The LLM layer must never break the loop: any error -> template fallback."""

    def __init__(self, primary: Any, fallback: Any) -> None:
        super().__init__(primary, fallback, catch_all=True)

    def explain_decision(self, decision): return self.__getattr__("explain_decision")(decision)
    def _opt(self, name: str, args: tuple, kw: dict) -> Any:
        """Per-call fallback that forwards optional kwargs only to implementations whose signature accepts them."""
        p = getattr(self._primary, name) if self._primary is not None else None
        f = getattr(self._fallback, name)
        if p is not None:
            try:
                return call_with_supported(p, *args, **kw)
            except Exception as e:  # NotImplementedError (stub) or any LLM error -> template
                if not isinstance(e, NotImplementedError):
                    log.exception("%s.%s failed, using fallback", type(self._primary).__name__, name)
        self.fallback_methods.add(name)
        return call_with_supported(f, *args, **kw)

    def summarize_state(self, status, **kw): return self._opt("summarize_state", (status,), kw)
    def parse_strategy_from_text(self, text, default_symbol=None): return self.__getattr__("parse_strategy_from_text")(text, default_symbol)
    def answer(self, question, status, **kw): return self._opt("answer", (question, status), kw)


def _probe_risk(r: Any) -> bool:
    try:
        sig = Signal(symbol="NVDAon", action=Action.STAY_OUT, source=AgentMode.MANUAL, strategy="probe", reason="probe")
        r.check(sig, Portfolio(starting_capital=1000, cash=1000, equity=1000), r.get_control_state())
        r.check_exits(Portfolio(starting_capital=1000, cash=1000, equity=1000), {})
        return True
    except NotImplementedError:
        return False


def presets_table(engine: Any) -> tuple[str, list[StrategySelection]]:
    """Ask the wired engine's select_strategy for every (regime, style) with all presets enabled.
    Returns (source, selections); source says whether the real engine or the app fallback answered."""
    from backend.contracts import PresetName, Regime, TraderStyle

    rows = [engine.select_strategy(r, s, list(PresetName)) for r in Regime for s in TraderStyle]
    if isinstance(engine, _PerCallFallback):
        if engine._primary is None or "select_strategy" in engine.fallback_methods:
            return f"fallback:{type(engine._fallback).__name__}", rows
        return f"engine:{type(engine._primary).__name__}", rows
    n = type(engine).__name__
    return (f"fallback:{n}" if n.startswith("Fallback") else f"engine:{n}"), rows


@dataclass
class Components:
    engine: StrategyEngine
    risk: RiskManager
    exchange: ExchangeClient
    explainer: Explainer
    describe: Any = None
    backtest_engine: Any = None  # PAPER/LIVE: plain engine for backtests (the live engine adds live-only guards)

    def names(self) -> dict[str, str]:
        d = self.describe or (lambda o: type(o).__name__)
        return {k: d(getattr(self, k)) for k in ("engine", "risk", "exchange", "explainer")}


def build_components(settings: Settings, exchange: Optional[ExchangeClient] = None, engine: Any = None,
                     risk: Any = None, explainer: Any = None) -> Components:
    """Explicit arguments override discovery (used by tests)."""
    settings.validate()
    fb = settings.use_fallbacks_only
    backtest_engine = None
    if engine is None:
        from backend.engine.engine import DefaultStrategyEngine
        primary = None if fb else DefaultStrategyEngine()
        if not fb and settings.trading_mode != TradingMode.SIM:
            try:  # Bot 2 live engine: live-safety layer (stale data, token status, min order, ...)
                live_primary = DefaultStrategyEngine(live_safe=True, clock=utc_now)
                backtest_engine = ResilientEngine(primary, FallbackStrategyEngine())
                primary = live_primary
            except TypeError:  # older engine without live_safe
                pass
        engine = ResilientEngine(primary, FallbackStrategyEngine())
    if explainer is None:
        from backend.llm.explainer import DefaultExplainer
        explainer = ResilientExplainer(None if fb else DefaultExplainer(), FallbackExplainer())
    if risk is None:
        ctrl = ControlState(trading_mode=settings.trading_mode)
        risk = None
        if not fb:
            from backend.risk.manager import DefaultRiskManager
            cand = DefaultRiskManager(control=ctrl)
            risk = cand if _probe_risk(cand) else None
        risk = risk or FallbackRiskManager(control=ctrl, live_allowed=settings.live_enabled and settings.risk_allow_live)
    if exchange is None:
        from backend.exchange.factory import get_exchange_client
        exchange = get_exchange_client(settings.trading_mode)
        if settings.trading_mode == TradingMode.SIM and type(exchange).__name__ == "MockExchangeClient":
            from backend.mocks import MockExchangeClient
            exchange = MockExchangeClient(seed=settings.sim_seed)  # honour SIM_SEED for reproducible demos
        elif settings.trading_mode == TradingMode.SIM and callable(getattr(exchange, "set_replay_index", None)):
            try:  # Bot 4's SimulatedBinanceClient: same seed / replay length as the settings
                exchange = type(exchange)(seed=settings.sim_seed, n_candles=settings.sim_replay_bars)
            except TypeError:
                pass
    if exchange.mode != settings.trading_mode:
        raise RuntimeError(f"exchange client mode {exchange.mode.value} != TRADING_MODE {settings.trading_mode.value}")
    for obj, proto in ((engine, StrategyEngine), (risk, RiskManager), (exchange, ExchangeClient), (explainer, Explainer)):
        if not isinstance(obj, proto):
            raise TypeError(f"{type(obj).__name__} does not satisfy {proto.__name__}")

    def name(o: Any) -> str:
        if hasattr(o, "describe"):
            return o.describe()
        n = type(o).__name__
        return f"fallback:{n}" if n.startswith("Fallback") else n

    return Components(engine=engine, risk=risk, exchange=exchange, explainer=explainer, describe=name,
                      backtest_engine=backtest_engine)
