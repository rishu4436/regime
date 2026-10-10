from datetime import datetime, timezone

import pytest

from backend.contracts import (
    Action,
    AgentMode,
    BreakoutDirection,
    ConditionLogic,
    ExitReason,
    ManualStrategy,
    MarketHoursFilter,
    Portfolio,
    Position,
    StrategyEngine,
)
from backend.engine import DefaultStrategyEngine
from backend.engine.manual import ManualIndicators, evaluate_entry, in_market_hours

from .helpers import ENTRY_BAR, bar, entry_scenario, flat, sma_entry_strategy

ENG = DefaultStrategyEngine()


def pf(positions=(), bars_since_exit=None, equity=1000.0):
    return Portfolio(
        starting_capital=1000,
        cash=equity,
        equity=equity,
        positions=list(positions),
        bars_since_exit=bars_since_exit or {},
    )


def entry_candles():
    return entry_scenario([], tail=0)  # last bar = the entry bar (close 101 > SMA2)


def test_protocol_conformance():
    assert isinstance(ENG, StrategyEngine)


def test_buy_signal_with_reason_and_size():
    sig = ENG.evaluate_manual(sma_entry_strategy(), entry_candles(), pf())
    assert sig.action == Action.BUY and sig.source == AgentMode.MANUAL and sig.strategy == "MANUAL:test"
    assert sig.conditions_met == ["price_above_sma"] and sig.conditions_failed == []
    assert sig.proposed_size_pct == 50 and sig.proposed_notional == pytest.approx(500)
    assert sig.price == 101 and "SMA(2)" in sig.reason


def test_and_vs_or():
    s = sma_entry_strategy()
    s.entry.rsi_below.enabled = True  # RSI(14) after a flat series + uptick = 100 -> not < 35
    sig = ENG.evaluate_manual(s, entry_candles(), pf())
    assert sig.action == Action.STAY_OUT and sig.conditions_failed == ["rsi_below"]
    s.entry.logic = ConditionLogic.OR
    assert ENG.evaluate_manual(s, entry_candles(), pf()).action == Action.BUY


def test_warmup_conditions_are_skipped_not_false():
    s = sma_entry_strategy()
    s.entry.price_above_ema.enabled = True
    s.entry.price_above_ema.period = 500  # never available
    cs = entry_candles()
    ev = evaluate_entry(s, ManualIndicators.compute(s, cs), cs, len(cs) - 1)
    assert ev.ok and ev.skipped == ["price_above_ema"]
    s.entry.price_above_sma.enabled = False
    sig = ENG.evaluate_manual(s, cs, pf())
    assert sig.action == Action.STAY_OUT and "warming up" in sig.reason


def test_no_conditions_enabled():
    s = sma_entry_strategy()
    s.entry.price_above_sma.enabled = False
    sig = ENG.evaluate_manual(s, entry_candles(), pf())
    assert sig.action == Action.STAY_OUT and "no entry conditions" in sig.reason


def test_cooldown_and_max_positions():
    s = sma_entry_strategy()
    s.risk.cooldown_bars = 5
    assert ENG.evaluate_manual(s, entry_candles(), pf(bars_since_exit={"NVDAon": 5})).action == Action.STAY_OUT
    assert ENG.evaluate_manual(s, entry_candles(), pf(bars_since_exit={"NVDAon": 6})).action == Action.BUY
    assert ENG.evaluate_manual(s, entry_candles(), pf(bars_since_exit={"OTHER": 0})).action == Action.BUY
    pos = Position(symbol="NVDAon", quantity=1, avg_entry_price=101, strategy="MANUAL:test")
    sig = ENG.evaluate_manual(s, entry_candles(), pf([pos]))
    assert sig.action == Action.STAY_OUT and sig.conditions_failed == ["max_open_positions"]


def test_sell_on_exit_rules():
    s = sma_entry_strategy()
    pos = Position(symbol="NVDAon", quantity=2, avg_entry_price=101, strategy="MANUAL:test")
    cs = entry_scenario([(101, 115, 94, 110)], tail=0)  # same-bar SL+TP
    sig = ENG.evaluate_manual(s, cs, pf([pos]))
    assert sig.action == Action.SELL and sig.conditions_met == [ExitReason.STOP_LOSS.value]
    assert sig.proposed_notional == pytest.approx(2 * 110)
    cs = entry_scenario([(120, 121, 119, 120)], tail=0)
    assert ENG.evaluate_manual(s, cs, pf([pos])).conditions_met == [ExitReason.TAKE_PROFIT.value]


def test_trailing_uses_position_high_water_mark():
    s = sma_entry_strategy()
    s.exit.trailing_stop.enabled = True
    s.exit.trailing_stop.pct = 4
    pos = Position(symbol="NVDAon", quantity=1, avg_entry_price=101, strategy="MANUAL:test", highest_price=110)
    cs = entry_scenario([(108, 108.5, 105, 106)], tail=0)  # low 105 <= 110*0.96 = 105.6
    sig = ENG.evaluate_manual(s, cs, pf([pos]))
    assert sig.action == Action.SELL and sig.conditions_met == ["TRAILING_STOP"]


def test_rsi_exit_signal():
    s = sma_entry_strategy()
    s.exit.rsi_above.enabled = True
    pos = Position(symbol="NVDAon", quantity=1, avg_entry_price=101, strategy="MANUAL:test")
    cs = flat(30) + [
        bar(30 + k, 100 + k * 0.5, 100 + (k + 1) * 0.5, 100 + k * 0.5, 100 + (k + 1) * 0.5) for k in range(10)
    ]
    sig = ENG.evaluate_manual(s, cs, pf([pos]))
    assert sig.action == Action.SELL and sig.conditions_met == ["RSI_EXIT"]


def test_market_hours_and_min_volume_block_entries():
    s = sma_entry_strategy()
    s.filters.market_hours.enabled = True
    cs = entry_candles()  # last bar is index 60 -> Jan 7 12:00 UTC (Wednesday)
    assert cs[-1].timestamp.hour == 12
    sig = ENG.evaluate_manual(s, cs, pf())
    assert sig.action == Action.STAY_OUT and sig.conditions_failed == ["market_hours"]
    s.filters.market_hours.enabled = False
    s.filters.min_volume.enabled = True
    sig = ENG.evaluate_manual(s, cs, pf())  # volume equals its average -> not above 1.1x
    assert sig.action == Action.STAY_OUT and sig.conditions_failed == ["min_volume"]
    cs[-1] = bar(ENTRY_BAR, 100, 101, 100, 101, v=5000)
    assert ENG.evaluate_manual(s, cs, pf()).action == Action.BUY
    s.filters.min_volume.min_volume = 6000  # absolute minimum
    assert ENG.evaluate_manual(s, cs, pf()).action == Action.STAY_OUT


def test_in_market_hours():
    f = MarketHoursFilter(enabled=True)
    wed = lambda h: datetime(2026, 1, 7, h, tzinfo=timezone.utc)  # noqa: E731
    assert not in_market_hours(wed(12), f)
    assert in_market_hours(wed(13), f) and in_market_hours(wed(19), f)
    assert not in_market_hours(wed(20), f)
    assert not in_market_hours(datetime(2026, 1, 10, 15, tzinfo=timezone.utc), f)  # Saturday
    assert in_market_hours(datetime(2026, 1, 10, 15, tzinfo=timezone.utc), MarketHoursFilter(weekdays_only=False))
    wrap = MarketHoursFilter(start_hour_utc=22, end_hour_utc=2, weekdays_only=False)
    assert in_market_hours(wed(23), wrap) and in_market_hours(wed(1), wrap) and not in_market_hours(wed(3), wrap)
    assert in_market_hours(datetime(2026, 1, 7, 14), f)  # naive timestamps are treated as UTC


def test_breakout_condition():
    s = ManualStrategy(name="bo")
    s.entry.rsi_below.enabled = False
    s.entry.price_above_sma.enabled = False
    s.entry.breakout.enabled = True
    s.entry.breakout.lookback = 5
    cs = flat(30) + [bar(30, 100, 102, 100, 101.5)]
    assert ENG.evaluate_manual(s, cs, pf()).conditions_met == ["breakout"]
    cs = flat(30) + [bar(30, 100, 100, 99, 99.5)]
    s.entry.breakout.direction = BreakoutDirection.LOW
    assert ENG.evaluate_manual(s, cs, pf()).action == Action.BUY


def test_empty_candles():
    assert ENG.evaluate_manual(ManualStrategy(), [], pf()).action == Action.STAY_OUT
