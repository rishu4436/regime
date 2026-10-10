from backend.contracts import ControlState, RiskManager, TradingMode
from backend.risk import DefaultRiskManager, RiskConfig

from .conftest import T0


def test_conforms_to_protocol():
    assert isinstance(DefaultRiskManager(), RiskManager)


def test_defaults_are_safe(clock):
    rm = DefaultRiskManager(clock=clock)
    s = rm.get_control_state()
    assert not s.paused and not s.emergency_stopped and s.confirmation_mode and s.trading_mode == TradingMode.SIM
    assert s.updated_at == T0


def test_pause_resume(clock):
    rm = DefaultRiskManager(clock=clock)
    clock.advance(minutes=1)
    s = rm.pause("news event")
    assert s.paused and "news event" in s.reason and s.updated_at > T0
    s = rm.resume()
    assert not s.paused and s.reason == "Resumed"


def test_emergency_stop_requires_explicit_reset(clock):
    rm = DefaultRiskManager(clock=clock)
    s = rm.emergency_stop("wallet drained?")
    assert s.emergency_stopped and not s.paused and "EMERGENCY" in s.reason  # paused untouched
    s = rm.resume()
    assert s.emergency_stopped and not s.paused and "reset_emergency_stop" in s.reason  # resume never clears it
    s = rm.reset_emergency_stop()
    assert not s.emergency_stopped and not s.paused and "trading allowed" in s.reason


def test_reset_emergency_stop_leaves_paused_as_is(clock):
    rm = DefaultRiskManager(clock=clock)
    rm.pause("maintenance")
    rm.emergency_stop()
    s = rm.reset_emergency_stop()
    assert not s.emergency_stopped and s.paused and "still paused" in s.reason
    assert not rm.resume().paused


def test_reset_emergency_stop_is_interface_method():
    from backend.contracts.interfaces import RiskManager as P
    assert "reset_emergency_stop" in dir(P)


def test_confirmation_mode_toggle(clock):
    rm = DefaultRiskManager(clock=clock)
    assert not rm.set_confirmation_mode(False).confirmation_mode
    assert rm.set_confirmation_mode(True).confirmation_mode


def test_live_refused_unless_enabled(clock):
    rm = DefaultRiskManager(clock=clock)
    s = rm.set_trading_mode(TradingMode.LIVE)
    assert s.trading_mode == TradingMode.SIM and "refused" in s.reason
    assert rm.set_trading_mode(TradingMode.PAPER).trading_mode == TradingMode.PAPER
    rm2 = DefaultRiskManager(config=RiskConfig(allow_live=True), clock=clock)
    assert rm2.set_trading_mode(TradingMode.LIVE).trading_mode == TradingMode.LIVE


def test_initial_control_injected():
    rm = DefaultRiskManager(ControlState(paused=True))
    assert rm.get_control_state().paused
