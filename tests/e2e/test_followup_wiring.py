"""Follow-up wiring (SIM only): token-aware wallet limits (min_wallet_usdt / per_token), parser notes on
/api/llm/parse, risk.live_limits() pushed to the explainer, and the SIM replay clock across timeframe switches."""
from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from backend.app.fallbacks import FallbackExplainer
from backend.app.main import create_app
from backend.app.settings import Settings
from backend.app.wiring import ResilientExplainer, build_components
from backend.contracts import ManualStrategy, Timeframe

from .helpers import SEED, trading_strategy


def _activate(c: TestClient, symbol: str, tf: str = "1h") -> None:
    d = trading_strategy(symbol, activation="AUTO_EXECUTE").model_dump(mode="json")
    d["timeframe"] = tf
    r = c.post("/api/manual/activate", json={"strategy": d})
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------- wallet: token-aware minimums
def test_wallet_min_wallet_follows_active_token_minimum():
    s = Settings(sim_seed=SEED)
    comps = build_components(s)
    if not callable(getattr(comps.risk, "min_wallet_info", None)):
        pytest.skip("risk without min_wallet_info (needs bot3/live-fixes)")
    if not callable(getattr(comps.exchange, "get_token_status", None)):
        pytest.skip("exchange without get_token_status (Bot 4)")
    c = TestClient(create_app(s, comps))
    _activate(c, "NVDAon")
    lim = c.get("/api/wallet").json()["limits"]
    fee = lim.get("fee_buffer_usdt") or 0.0
    assert lim["limits_symbol"] == "NVDAon"
    assert lim["min_order_usdt"] == 20.0 and lim["min_wallet_usdt"] == pytest.approx(20.0 + fee)
    per = lim["per_token"]
    assert per["NVDAon"]["min_order_usdt"] == 20.0 and per["NVDAon"]["min_wallet_usdt"] == pytest.approx(20.0 + fee)
    assert per["AAPLB"]["min_order_usdt"] == 10.0 and per["AAPLB"]["min_wallet_usdt"] == pytest.approx(10.0 + fee)


def test_wallet_per_token_without_risk_support_keeps_exchange_minimum():
    """A risk without min_wallet_info still gets per_token min_order_usdt from Bot 4's token status."""
    from backend.app.fallbacks import FallbackRiskManager
    s = Settings(sim_seed=SEED)
    comps = build_components(s, risk=FallbackRiskManager())
    if not callable(getattr(comps.exchange, "get_token_status", None)):
        pytest.skip("exchange without get_token_status (Bot 4)")
    c = TestClient(create_app(s, comps))
    per = c.get("/api/wallet").json()["limits"].get("per_token") or {}
    assert per.get("NVDAon", {}).get("min_order_usdt") == 20.0


# ---------------------------------------------------------------- LLM: parser notes + live limits
class _NotesExplainer(FallbackExplainer):
    def __init__(self):
        super().__init__()
        self.limits: list = []

    def parse_strategy_with_notes(self, text, default_symbol=None):
        return ManualStrategy(name="parsed", symbol=default_symbol or "AAPLB"), ["Entry rule: price above EMA(20) (default)"]

    def set_live_limits(self, limits):
        self.limits.append(limits)


def _llm_client(primary):
    s = Settings(sim_seed=SEED, use_fallbacks_only=True)
    comps = build_components(s, explainer=ResilientExplainer(primary, FallbackExplainer()))
    return TestClient(create_app(s, comps))


def test_parse_passes_parser_notes_through():
    c = _llm_client(_NotesExplainer())
    r = c.post("/api/llm/parse", json={"text": "buy AAPLB, sell at 12% profit", "default_symbol": "AAPLB"}).json()
    assert r["kind"] == "MANUAL" and r["notes"] == ["Entry rule: price above EMA(20) (default)"]


def test_parse_notes_default_empty():
    c = _llm_client(None)
    r = c.post("/api/llm/parse", json={"text": "buy AAPLB when RSI below 30", "default_symbol": "AAPLB"}).json()
    assert r["notes"] == []


def test_live_limits_pushed_to_explainer():
    ex = _NotesExplainer()
    c = _llm_client(ex)
    _activate(c, "AAPLB")
    c.post("/api/loop/tick")
    assert ex.limits and isinstance(ex.limits[-1], dict) and "per_order_cap_usdt" in ex.limits[-1]
    c.get("/api/wallet")
    assert "daily_limit_usdt" in ex.limits[-1]


# ---------------------------------------------------------------- SIM replay clock across timeframe switches
def test_timeframe_switch_keeps_replay_clock():
    s = Settings(sim_seed=SEED)
    comps = build_components(s)
    if not callable(getattr(comps.exchange, "index_at", None)):
        pytest.skip("SIM client without index_at (needs Bot 4 aea22d1)")
    c = TestClient(create_app(s, comps))
    _activate(c, "AAPLB", "1h")
    for _ in range(30):
        c.post("/api/loop/tick")
    t_h1 = getattr(comps.exchange, "replay_time", None)
    st = c.get("/api/loop").json()
    assert t_h1 is not None, st
    _activate(c, "AAPLB", "4h")
    c.post("/api/loop/tick")
    t_h4 = comps.exchange.replay_time
    assert t_h1 < t_h4 <= t_h1 + timedelta(hours=4), (t_h1, t_h4)  # the next 4h bar, not a jump to the warm-up end
    _activate(c, "AAPLB", "1h")
    c.post("/api/loop/tick")
    t_back = comps.exchange.replay_time
    assert t_h4 < t_back <= t_h4 + timedelta(hours=1), (t_h4, t_back)


def test_replay_feed_old_client_signature_still_works():
    """A client whose set_replay_index takes only the index (pre-aea22d1) keeps working."""
    from backend.app.feed import ReplayFeed
    from backend.mocks import MockExchangeClient

    class Old(MockExchangeClient):
        pinned: list = []

        def set_replay_index(self, i):
            self.pinned.append(i)

    ex = Old(seed=SEED, n_candles=300)
    f = ReplayFeed(ex, total_bars=300, window=50)
    assert f.next_window("AAPLB", Timeframe.H1) and ex.pinned
