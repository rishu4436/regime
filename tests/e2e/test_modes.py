"""SIM / PAPER / LIVE separation."""
from __future__ import annotations

import pytest

from backend.app.fallbacks import FallbackRiskManager
from backend.app.settings import LiveModeNotAllowed, Settings
from backend.app.wiring import build_components
from backend.contracts import Action, AgentMode, ControlState, Portfolio, Signal, TradingMode
from backend.mocks import MockExchangeClient


LIVE_ENV = ("TRADING_MODE", "LIVE_TRADING_ENABLED", "BINANCE_LIVE_ENABLED", "BINANCE_API_KEY", "BINANCE_API_SECRET")


def test_default_is_sim(monkeypatch):
    for k in LIVE_ENV:
        monkeypatch.delenv(k, raising=False)
    s = Settings.from_env()
    assert s.trading_mode == TradingMode.SIM and not s.live_enabled
    comps = build_components(s)
    assert comps.exchange.mode == TradingMode.SIM


def _live_env(monkeypatch, app_flag, exch_flag, key, secret):
    monkeypatch.setenv("TRADING_MODE", "LIVE")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true" if app_flag else "")
    monkeypatch.setenv("BINANCE_LIVE_ENABLED", "true" if exch_flag else "false")
    monkeypatch.setenv("BINANCE_API_KEY", "k" if key else "")
    monkeypatch.setenv("BINANCE_API_SECRET", "s" if secret else "")


@pytest.mark.parametrize("app_flag,exch_flag,key,secret", [
    (a, b, k, s) for a in (False, True) for b in (False, True) for k in (False, True) for s in (False, True)
    if not (a and b and k and s)])
def test_live_refused_unless_both_flags_and_keys(monkeypatch, app_flag, exch_flag, key, secret):
    _live_env(monkeypatch, app_flag, exch_flag, key, secret)
    with pytest.raises(LiveModeNotAllowed) as ei:
        build_components(Settings.from_env())
    msg = str(ei.value)
    missing = msg.split("missing:")[1].split(".")[0]
    expect = {"LIVE_TRADING_ENABLED=true": app_flag, "BINANCE_LIVE_ENABLED=true": exch_flag,
              "BINANCE_API_KEY": key, "BINANCE_API_SECRET": secret}
    for name, present in expect.items():  # names exactly what is missing, nothing else
        assert (name in missing) == (not present), (name, msg)
    assert "=s" not in msg and "'s'" not in msg  # never echoes secret values


def test_live_passes_gate_with_both_flags_and_keys(monkeypatch):
    _live_env(monkeypatch, True, True, True, True)
    s = Settings.from_env()
    s.validate()  # the app gate passes; actually trading LIVE is then up to Bot 4's factory (executor, wallet, ...)
    assert s.live_enabled and s.live_missing() == []


def test_one_flag_alone_never_enables_live():
    for app_flag, exch_flag in ((True, False), (False, True)):
        s = Settings(trading_mode=TradingMode.LIVE, live_trading_enabled=app_flag, binance_live_enabled=exch_flag, has_binance_keys=True)
        assert not s.live_enabled
        with pytest.raises(LiveModeNotAllowed, match="BINANCE_LIVE_ENABLED=true" if app_flag else "LIVE_TRADING_ENABLED=true"):
            s.validate()


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


@pytest.mark.parametrize("app_flag,exch_flag", [("true", ""), ("", "true"), ("true", "true")])
def test_fallback_risk_live_env_needs_both_flags(monkeypatch, app_flag, exch_flag):
    monkeypatch.setenv("LIVE_TRADING_ENABLED", app_flag)
    monkeypatch.setenv("BINANCE_LIVE_ENABLED", exch_flag)
    sig = Signal(symbol="NVDAon", action=Action.BUY, source=AgentMode.MANUAL, strategy="x", reason="x", proposed_notional=100)
    pf = Portfolio(starting_capital=1000, cash=1000, equity=1000)
    ctrl = ControlState(trading_mode=TradingMode.LIVE, confirmation_mode=False)
    assert FallbackRiskManager().check(sig, pf, ctrl).approved == (app_flag == exch_flag == "true")


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
