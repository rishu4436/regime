from datetime import timedelta

import pytest
from pydantic import ValidationError

from backend.contracts import (
    Action, AgentMode, AutoConfig, Candle, ConditionLogic, ControlState, Decision, ExchangeClient,
    Explainer, ManualStrategy, PresetName, Quote, OrderSide, Regime, RiskManager, RiskVerdict, Signal,
    StrategyEngine, TraderStyle, TradingMode, utc_now,
)


def test_manual_strategy_defaults_match_reference_simulator():
    s = ManualStrategy()
    assert s.symbol == "NVDAon"
    assert s.entry.logic == ConditionLogic.AND
    assert s.entry.rsi_below.enabled and s.entry.rsi_below.threshold == 35 and s.entry.rsi_below.period == 14
    assert s.entry.price_above_sma.enabled and s.entry.price_above_sma.period == 50
    assert not s.entry.price_above_ema.enabled and s.entry.price_above_ema.period == 20
    assert not s.entry.macd_bullish_cross.enabled and not s.entry.bollinger_lower.enabled
    assert s.entry.bollinger_lower.proximity_pct == 2.0 and s.entry.volume_above_avg.multiplier == 1.1
    assert s.exit.take_profit_pct == 12 and s.exit.stop_loss_pct == 6
    assert not s.exit.trailing_stop.enabled and s.exit.trailing_stop.pct == 4
    assert s.exit.rsi_above.enabled and s.exit.rsi_above.threshold == 70
    assert not s.exit.macd_bearish_cross.enabled
    assert s.risk.position_size_pct == 15 and s.risk.max_open_positions == 1 and s.risk.cooldown_bars == 5
    assert not s.filters.market_hours.enabled and not s.filters.min_volume.enabled


def test_manual_strategy_json_roundtrip_and_strict():
    s = ManualStrategy(name="dip", symbol="TSLAx")
    again = ManualStrategy.model_validate_json(s.model_dump_json())
    assert again == s
    with pytest.raises(ValidationError):
        ManualStrategy.model_validate({"symbol": "X", "unknown_field": 1})


def test_candle_validation():
    now = utc_now()
    Candle(timestamp=now, open=1, high=2, low=0.5, close=1.5, volume=10)
    with pytest.raises(ValidationError):
        Candle(timestamp=now, open=1, high=0.9, low=0.5, close=1.5, volume=10)


def test_quote_ttl():
    q = Quote(symbol="NVDAon", side=OrderSide.BUY, notional=100, quantity=0.7, price=140)
    assert (q.expires_at - q.created_at) == timedelta(seconds=30) or abs((q.expires_at - q.created_at).total_seconds() - 30) < 1
    assert not q.is_expired()
    assert q.is_expired(q.expires_at + timedelta(seconds=1))


def test_auto_config_defaults_all_presets():
    c = AutoConfig(symbol="NVDAon", capital=1000)
    assert set(c.enabled_presets) == set(PresetName) and c.style == TraderStyle.BALANCED


def test_decision_composes():
    sig = Signal(symbol="NVDAon", action=Action.STAY_OUT, source=AgentMode.AUTO, strategy="GRID", regime=Regime.SIDEWAYS, reason="no grid level hit")
    v = RiskVerdict(signal_id=sig.id, approved=False, reasons=["paused"])
    d = Decision(mode=TradingMode.SIM, agent_mode=AgentMode.AUTO, symbol="NVDAon", regime=Regime.SIDEWAYS,
                 strategy="GRID", action=Action.STAY_OUT, reason="paused", signal=sig, risk_verdict=v)
    assert Decision.model_validate_json(d.model_dump_json()) == d


def test_enums_complete():
    assert len(Regime) == 6 and len(TraderStyle) == 3 and len(PresetName) == 5
    assert ControlState().trading_mode == TradingMode.SIM and ControlState().confirmation_mode


def test_protocols_are_runtime_checkable():
    for p in (ExchangeClient, StrategyEngine, RiskManager, Explainer):
        assert hasattr(p, "__protocol_attrs__") or hasattr(p, "_is_protocol")


def test_v02_additions_backward_compatible():
    from backend.contracts import (
        Alert, AlertLevel, BacktestRequest, ExitReason, LoopState, PerformanceSummary, Position, SystemInfo,
    )
    p1, p2 = Position(symbol="NVDAon", quantity=1, avg_entry_price=140, strategy="GRID"), Position(symbol="NVDAon", quantity=1, avg_entry_price=140, strategy="GRID")
    assert p1.id and p1.id != p2.id
    s = Signal(symbol="NVDAon", action=Action.SELL, source=AgentMode.AUTO, strategy="GRID", reason="sl",
               exit_reason=ExitReason.STOP_LOSS, position_id=p1.id, price=131.6)
    assert Signal.model_validate_json(s.model_dump_json()) == s
    assert Signal(symbol="X", action=Action.BUY, source=AgentMode.MANUAL, strategy="MANUAL:x", reason="r").exit_reason is None
    assert Alert(level=AlertLevel.CRITICAL, code="EMERGENCY_STOP", message="stop").id
    assert BacktestRequest(strategy=ManualStrategy()).initial_capital == 10000 and BacktestRequest(strategy=ManualStrategy()).limit == 500
    LoopState(running=False, interval_seconds=1.0, ticks=0)
    SystemInfo(trading_mode=TradingMode.SIM, live_enabled=False, contracts_version="0.2.0", components={"engine": "mock"})
    PerformanceSummary(starting_capital=1, equity=1, realized_pnl=0, unrealized_pnl=0, total_return_pct=0, max_drawdown_pct=0,
                       win_rate_pct=0, num_trades=0, wins=0, losses=0, consecutive_losses=0)
