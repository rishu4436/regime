"""Small-wallet LIVE matrix (no network, no real money, never spawns baw).

A LIVE-reporting SimulatedBinanceClient (Bot 4's SIM token statuses + min orders: NVDAB 10 USDT, NVDAon/Ondo 20 USDT)
runs through the real wiring with the real engine (live_safe) + real risk (allow_live via RiskConfig, not env), on
wallets of 30 / 100 / 500 USDT:

- 100 and 500 USDT: at least one BUY per token FILLED, order notional >= the token minimum and <= the per-order cap.
- 30 USDT: a BUY is either filled at (or above) the minimum or blocked with a clear reason - never a FAILED execution.
- Weekend clock: NVDAon (Ondo OFFHOURS) stays out with live_token_paused / a risk off-hours block; NVDAB still trades.

With bot2/followup (engine raises to get_token_status().min_order_usdt) + bot3/live-fixes (risk raises small buys to the
token minimum) every funded case fills at the token minimum (NVDAB 10, NVDAon 20). 30 USDT may fill at the minimum or
block, always with a clear reason.
"""
from __future__ import annotations

import hashlib
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest
from fastapi.testclient import TestClient

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.app.wiring import build_components
from backend.contracts import ControlState, OrderSide, OrderStatus, Timeframe, TradingMode, utc_now

from .helpers import SEED, trading_strategy

WEEKDAY = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)  # Wed: Ondo TRADING
WEEKEND = datetime(2026, 10, 11, 10, 0, tzinfo=timezone.utc)  # Sun: Ondo OFFHOURS
TOKEN_MIN = {"NVDAB": 10.0, "NVDAon": 20.0}


def _real_impls():
    try:
        from backend.engine.engine import DefaultStrategyEngine
        from backend.exchange.sim import SimulatedBinanceClient
        from backend.risk import DefaultRiskManager, LiveContext, RiskConfig  # noqa: F401
        DefaultStrategyEngine(live_safe=True, clock=utc_now)
        return SimulatedBinanceClient
    except Exception:
        return None


SimClient = _real_impls()
pytestmark = pytest.mark.skipif(SimClient is None, reason="real engine (live_safe) / risk LiveContext / SIM client not importable")


if SimClient is not None:
    class LiveSim(SimClient):  # type: ignore[misc, valid-type]
        """Bot 4 SIM client reporting mode LIVE. Token status is evaluated on `status_clock` (weekday/weekend);
        quotes/fills use the real clock so TTL checks hold. Candles are re-timed so the current bar is fresh."""

        def __init__(self, usdt: float, status_clock: datetime, bnb: float = 0.05):
            super().__init__(seed=SEED, n_candles=260)
            self._mode = TradingMode.LIVE
            self.sim_wallet_usdt, self.sim_wallet_bnb = float(usdt), float(bnb)
            self.wallet_address = "0x2222222222222222222222222222222222222222"
            self.status_clock = status_clock
            self.cursor = 230
            self._anchor = utc_now().replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
            self.order_notional: dict[str, float] = {}

        def get_token_status(self, symbol):
            real, self._clock = self._clock, (lambda: self.status_clock)
            try:
                return super().get_token_status(symbol)
            finally:
                self._clock = real

        def get_candles(self, symbol, timeframe=Timeframe.H1, limit=200):
            full = super().get_candles(symbol, timeframe, limit=self._n)[:self.cursor]
            shift = self._anchor - full[-1].timestamp
            return [c.model_copy(update={"timestamp": c.timestamp + shift}) for c in full[-limit:]]

        def get_price(self, symbol):
            if symbol in self._price_override:
                return self._price_override[symbol]
            return super().get_candles(symbol, Timeframe.H1, limit=self._n)[self.cursor - 1].close

        def advance(self):
            self.cursor += 1
            self._anchor += timedelta(hours=1)

        def _execute(self, order, quote, path):
            self.order_notional[order.client_order_id] = quote.notional
            res = super()._execute(order.model_copy(update={"mode": TradingMode.PAPER}), quote, path)
            h = "0x" + hashlib.sha256(res.client_order_id.encode()).hexdigest()
            res = res.model_copy(update={"mode": TradingMode.LIVE, "tx_hash": h if res.tx_hash else None})
            self.executions[-1] = res
            return res


def _client(usdt: float, status_clock: datetime, monkeypatch) -> tuple[TestClient, "LiveSim"]:
    from backend.risk import DefaultRiskManager, RiskConfig
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("subprocess (baw) in test")))
    for k in ("BINANCE_API_KEY", "BINANCE_SECRET_KEY", "BINANCE_API_SECRET", "XAI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    s = Settings(trading_mode=TradingMode.LIVE, live_trading_enabled=True, binance_live_enabled=True, has_binance_keys=True,
                 risk_allow_live=True, starting_capital=usdt, sim_seed=SEED, use_fallbacks_only=False)
    ex = LiveSim(usdt, status_clock)
    risk = DefaultRiskManager(control=ControlState(trading_mode=TradingMode.LIVE, confirmation_mode=False),
                              config=RiskConfig(allow_live=True))
    c = TestClient(create_app(s, build_components(s, exchange=ex, risk=risk)))
    c.app.state.st.loop.wallet_settings.runner = lambda cmd: (_ for _ in ()).throw(AssertionError("baw in test"))
    c.post("/api/control/confirmation-mode", json={"enabled": False})
    return c, ex


def _buy_strategy(symbol: str) -> dict:
    d = trading_strategy(symbol, activation="AUTO_EXECUTE").model_dump(mode="json")
    d["entry"]["logic"] = "OR"
    d["entry"]["rsi_below"].update(enabled=True, threshold=100)  # enter on the first ready bar
    d["entry"]["bollinger_lower"]["enabled"] = False
    d["risk"]["position_size_pct"] = 15.0
    d["exit"].update(stop_loss_pct=6.0, take_profit_pct=50.0)
    d["exit"]["rsi_above"]["enabled"] = False
    return d


def _run(c: TestClient, ex: "LiveSim", symbol: str, ticks: int = 3) -> list[dict]:
    r = c.post("/api/manual/activate", json={"strategy": _buy_strategy(symbol)})
    assert r.status_code == 200, r.text
    out: list[dict] = []
    for _ in range(ticks):
        out += c.post("/api/loop/tick").json()
        ex.advance()
    return [d for d in out if d["symbol"] == symbol]


def _buy_attempts(ds: list[dict]) -> list[dict]:
    """Decisions where a BUY was proposed (executed or blocked)."""
    return [d for d in ds if d["action"] == "BUY" or (d.get("signal") or {}).get("action") == "BUY"
            or "live_" in (d.get("reason") or "")]


def _never_failed(c: TestClient, ex: "LiveSim", ds: list[dict]) -> None:
    for d in ds:
        e = d.get("execution") or {}
        assert e.get("status") not in ("FAILED", "REJECTED"), (d["reason"], e)
    assert not [r for r in ex.executions if r.side == OrderSide.BUY and r.status != OrderStatus.FILLED], ex.executions
    assert not [a for a in c.get("/api/alerts").json() if a["code"] == "EXECUTION_FAILED"]


def _filled_buys(ds: list[dict]) -> list[dict]:
    return [d for d in ds if d["action"] == "BUY" and (d.get("execution") or {}).get("status") == "FILLED"]


# ---------------------------------------------------------------- always-on invariants (green on main)
@pytest.mark.parametrize("usdt", [30, 100, 500])
@pytest.mark.parametrize("symbol", ["NVDAB", "NVDAon"])
def test_small_wallet_never_fails_and_reports_token_minimum(usdt, symbol, monkeypatch):
    c, ex = _client(usdt, WEEKDAY, monkeypatch)
    assert ex.get_token_status(symbol).min_order_usdt == TOKEN_MIN[symbol]
    ds = _run(c, ex, symbol)
    assert ds, "no decisions"
    _never_failed(c, ex, ds)
    cap = c.get("/api/wallet").json()["limits"]["per_order_cap_usdt"]
    for d in _filled_buys(ds):  # whatever fills today already respects the venue minimum and the cap
        n = ex.order_notional[d["execution"]["client_order_id"]]
        assert TOKEN_MIN[symbol] - 1e-6 <= n <= cap + 1e-6, (n, cap)
    for d in ds:
        if d["action"] != "BUY" and (d.get("signal") or {}).get("action") == "BUY":
            assert d["reason"].strip(), d  # a blocked BUY always says why


def test_engine_gets_token_minimum_in_market_context(monkeypatch):
    c, ex = _client(100, WEEKDAY, monkeypatch)
    _run(c, ex, "NVDAon", ticks=1)
    eng = c.app.state.st.loop.c.engine
    eng = getattr(eng, "_primary", None) or eng
    seen = {}
    orig = eng.set_market_context
    eng.set_market_context = lambda sym, ctx: (seen.__setitem__(sym, ctx), orig(sym, ctx))
    c.post("/api/loop/tick")
    assert seen.get("NVDAon", {}).get("min_order_usdt") == 20.0


# ---------------------------------------------------------------- pending bot2/followup + bot3/live-fixes
@pytest.mark.parametrize("usdt", [100, 500])
@pytest.mark.parametrize("symbol", ["NVDAB", "NVDAon"])
def test_funded_wallet_fills_at_least_one_buy(usdt, symbol, monkeypatch):
    c, ex = _client(usdt, WEEKDAY, monkeypatch)
    ds = _run(c, ex, symbol)
    _never_failed(c, ex, ds)
    fills = _filled_buys(ds)
    assert fills, [d["reason"] for d in ds]
    cap = c.get("/api/wallet").json()["limits"]["per_order_cap_usdt"]
    for d in fills:
        n = ex.order_notional[d["execution"]["client_order_id"]]
        assert TOKEN_MIN[symbol] - 1e-6 <= n <= cap + 1e-6, (n, cap)


@pytest.mark.parametrize("symbol", ["NVDAB", "NVDAon"])
def test_30_usdt_fills_at_minimum_or_blocks_with_clear_reason(symbol, monkeypatch):
    c, ex = _client(30, WEEKDAY, monkeypatch)
    ds = _run(c, ex, symbol)
    _never_failed(c, ex, ds)
    fills = _filled_buys(ds)
    if fills:
        for d in fills:
            assert ex.order_notional[d["execution"]["client_order_id"]] >= TOKEN_MIN[symbol] - 1e-6
    else:
        blocked = [d for d in ds if (d.get("signal") or {}).get("action") == "BUY" or "min" in d["reason"].lower()]
        assert blocked and all(any(k in d["reason"].lower() for k in ("min", "wallet", "balance", "cap", "below"))
                               for d in blocked), [d["reason"] for d in ds]


# ---------------------------------------------------------------- weekend: Ondo off-hours, bStock 24/7
def test_weekend_ondo_stays_out_never_fails(monkeypatch):
    c, ex = _client(500, WEEKEND, monkeypatch)
    assert ex.get_token_status("NVDAon").trading_status.value.upper() == "OFFHOURS"
    ds = _run(c, ex, "NVDAon")
    assert ds
    _never_failed(c, ex, ds)
    assert not [d for d in ds if d["action"] == "BUY"], [d["reason"] for d in ds]
    for d in ds:  # engine live_token_paused (status offhours) or a risk token-status / off-hours block
        why = (d["reason"] + " " + " ".join((d.get("signal") or {}).get("conditions_failed") or [])).lower()
        assert any(k in why for k in ("live_token_paused", "live_token_status", "offhours", "off-hours")), why


def test_weekend_nvdab_still_trades(monkeypatch):
    c, ex = _client(500, WEEKEND, monkeypatch)
    ds = _run(c, ex, "NVDAB")
    _never_failed(c, ex, ds)
    assert _filled_buys(ds), [d["reason"] for d in ds]
