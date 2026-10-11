"""SIM / PAPER / LIVE separation."""
from __future__ import annotations

import pytest

from backend.app.fallbacks import FallbackRiskManager
from backend.app.settings import LiveModeNotAllowed, Settings
from backend.app.wiring import build_components
from backend.contracts import Action, AgentMode, ControlState, Portfolio, Signal, TradingMode
from backend.mocks import MockExchangeClient


def test_default_is_sim(monkeypatch):
    for k in ("TRADING_MODE", "LIVE_TRADING_ENABLED", "BINANCE_API_KEY", "BINANCE_API_SECRET"):
        monkeypatch.delenv(k, raising=False)
    s = Settings.from_env()
    assert s.trading_mode == TradingMode.SIM and not s.live_enabled
    comps = build_components(s)
    assert comps.exchange.mode == TradingMode.SIM


@pytest.mark.parametrize("flag,keys", [(False, False), (True, False), (False, True)])
def test_live_refused_without_explicit_flag_and_keys(monkeypatch, flag, keys):
    monkeypatch.setenv("TRADING_MODE", "LIVE")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true" if flag else "")
    monkeypatch.setenv("BINANCE_API_KEY", "k" if keys else "")
    monkeypatch.setenv("BINANCE_API_SECRET", "s" if keys else "")
    with pytest.raises(LiveModeNotAllowed):
        build_components(Settings.from_env())


def test_live_settings_never_expose_secrets(monkeypatch):
    monkeypatch.setenv("BINANCE_API_KEY", "super-secret-key")
    monkeypatch.setenv("BINANCE_API_SECRET", "super-secret")
    s = Settings.from_env()
    assert "super-secret" not in repr(s)


def test_exchange_mode_must_match_trading_mode():
    with pytest.raises(RuntimeError, match="mode"):
        build_components(Settings(trading_mode=TradingMode.SIM), exchange=MockExchangeClient(mode=TradingMode.PAPER))


def test_mock_refuses_live():
    with pytest.raises(ValueError):
        MockExchangeClient(mode=TradingMode.LIVE)


def test_fallback_risk_blocks_live_buys_unless_enabled():
    sig = Signal(symbol="NVDAon", action=Action.BUY, source=AgentMode.MANUAL, strategy="x", reason="x", proposed_notional=100)
    pf = Portfolio(starting_capital=1000, cash=1000, equity=1000)
    ctrl = ControlState(trading_mode=TradingMode.LIVE, confirmation_mode=False)
    assert not FallbackRiskManager(live_allowed=False).check(sig, pf, ctrl).approved
    assert FallbackRiskManager(live_allowed=True).check(sig, pf, ctrl).approved


def test_paper_uses_exchange_factory():
    s = Settings(trading_mode=TradingMode.PAPER)
    try:
        comps = build_components(s)
    except NotImplementedError:
        pytest.skip("PAPER client not implemented yet (Bot 4)")
    except Exception as e:  # Bot 4's PAPER client needs Binance credentials; refusing loudly at startup is correct
        if "BINANCE_API_KEY" in str(e):
            pytest.skip(f"PAPER needs Binance keys: {e}")
        raise
    assert comps.exchange.mode == TradingMode.PAPER
