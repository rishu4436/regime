"""Exit semantics: contracts v0.2 ExitRules + Bot 3's check_exits edge rules. Bot 6 parity-tests these exact rules
(tests/e2e/test_exit_parity.py on bot6/main); the CASES below mirror its hand-made fixtures."""

from datetime import datetime, timedelta, timezone

import pytest

from backend.contracts import SIM_WARMUP_BARS, Candle, ExitReason, ManualStrategy, TrailingStopRule
from backend.engine import DefaultStrategyEngine
from backend.engine.exits import check_price_exit, max_loss_level, trail_from_hwm

from .helpers import ENTRY_BAR, ENTRY_PRICE, entry_scenario, falling_after_entry, sma_entry_strategy

ENG = DefaultStrategyEngine()
TP = ENTRY_PRICE * 1.12  # 113.12
SL = ENTRY_PRICE * 0.94  # 94.94


# ---------------------------------------------------------------- unit level (entry 101, TP 12%, SL 6%)
def test_same_bar_sl_and_tp_sl_wins():
    pe = check_price_exit(101, 115, 94, 101, 12, 6)
    assert pe.reason == ExitReason.STOP_LOSS and pe.price == pytest.approx(SL) and not pe.gap


def test_gap_down_through_stop_fills_at_open():
    pe = check_price_exit(90, 92, 89, 101, 12, 6)
    assert pe.reason == ExitReason.STOP_LOSS and pe.price == 90 and pe.gap


def test_gap_up_through_tp_fills_at_open():
    pe = check_price_exit(120, 121, 119, 101, 12, 6)
    assert pe.reason == ExitReason.TAKE_PROFIT and pe.price == 120 and pe.gap


def test_gap_up_open_but_low_hits_stop_stop_wins_at_level():
    pe = check_price_exit(120, 121, 94, 101, 12, 6)
    assert pe.reason == ExitReason.STOP_LOSS and pe.price == pytest.approx(SL) and not pe.gap


def test_open_exactly_at_level_fills_at_level():
    assert check_price_exit(SL, 100, 90, 101, 12, 6).price == pytest.approx(SL)
    pe = check_price_exit(TP, 120, 110, 101, 12, 6)
    assert pe.reason == ExitReason.TAKE_PROFIT and pe.price == pytest.approx(TP) and not pe.gap


def test_tp_only_fills_at_level_and_no_exit_inside_range():
    pe = check_price_exit(101, 114, 100, 101, 12, 6)
    assert pe.reason == ExitReason.TAKE_PROFIT and pe.price == pytest.approx(TP)
    assert check_price_exit(101, 113, 95, 101, 12, 6) is None


def test_trailing_beats_tp_same_bar():
    pe = check_price_exit(105, 115, 99, 101, 12, 6, trail_level=100.0)
    assert pe.reason == ExitReason.TRAILING_STOP and pe.price == 100.0


def test_max_loss_beats_tp_same_bar():
    pe = check_price_exit(101, 115, 97, 101, 12, 6, max_loss_level=98.0)
    assert pe.reason == ExitReason.MAX_LOSS and pe.price == 98.0


def test_highest_hit_stop_wins():
    # SL 94.94, trailing 96.96, max-loss 98 all hit -> max-loss (highest) at 98
    pe = check_price_exit(101, 102, 94, 101, 12, 6, trail_level=96.96, max_loss_level=98.0)
    assert pe.reason == ExitReason.MAX_LOSS and pe.price == 98.0
    # trailing above SL, both hit -> trailing at its level
    pe = check_price_exit(101, 102, 94, 101, 12, 6, trail_level=96.96)
    assert pe.reason == ExitReason.TRAILING_STOP and pe.price == 96.96
    # only the lower stop is hit? impossible on the way down; a higher level that is NOT hit is ignored:
    pe = check_price_exit(101, 102, 97.5, 101, 12, 6, trail_level=96.96, max_loss_level=98.0)
    assert pe.reason == ExitReason.MAX_LOSS


def test_gap_through_several_stops_fills_at_open_with_highest_reason():
    pe = check_price_exit(90, 91, 89, 101, 12, 6, trail_level=96.96, max_loss_level=95.5)
    assert pe.reason == ExitReason.TRAILING_STOP and pe.price == 90 and pe.gap


def test_trailing_is_strict_stop_loss_is_not():
    assert check_price_exit(101, 102, 96.96, 101, 12, 6, trail_level=96.96) is None  # low == trail -> no exit
    pe = check_price_exit(101, 102, SL, 101, 12, 6)  # low == SL -> exit
    assert pe.reason == ExitReason.STOP_LOSS


def test_ties_trailing_over_stop_loss_over_max_loss():
    pe = check_price_exit(101, 102, 90, 100, 50, 4, trail_level=96.0, max_loss_level=96.0)
    assert pe.reason == ExitReason.TRAILING_STOP
    pe = check_price_exit(101, 102, 90, 100, 50, 4, max_loss_level=96.0)
    assert pe.reason == ExitReason.STOP_LOSS


def test_explicit_position_levels_override_pcts():
    pe = check_price_exit(101, 102, 99, 101, 12, 6, stop_loss_price=99.5)
    assert pe.reason == ExitReason.STOP_LOSS and pe.price == 99.5
    pe = check_price_exit(101, 103, 100, 101, 12, 6, take_profit_price=102.0)
    assert pe.reason == ExitReason.TAKE_PROFIT and pe.price == 102.0


def test_helpers():
    assert trail_from_hwm(110, 4) == pytest.approx(105.6)
    assert max_loss_level(100, 10, 1000, 2) == pytest.approx(98)
    assert max_loss_level(100, 10, 1000, None) is None
    assert max_loss_level(100, 1, 1000, 50) is None  # unreachable (<= 0)


# ---------------------------------------------------------------- inside backtest_manual
def _only_trade(cs, strategy=None, capital=10_000):
    res = ENG.backtest_manual(strategy or sma_entry_strategy(), cs, capital)
    assert res.num_trades == 1, [t.exit_reason for t in res.trades]
    t = res.trades[0]
    assert t.entry_time == cs[ENTRY_BAR].timestamp and t.entry_price == ENTRY_PRICE
    return t


def _trail(pct=4.0):
    return sma_entry_strategy(trailing_stop=TrailingStopRule(enabled=True, pct=pct))


def test_backtest_same_bar_sl_tp():
    cs = entry_scenario([(101, 115, 94, 110)])
    t = _only_trade(cs)
    assert (t.exit_time, t.exit_reason) == (cs[ENTRY_BAR + 1].timestamp, ExitReason.STOP_LOSS)
    assert t.exit_price == pytest.approx(SL)


def test_backtest_gap_down_through_sl():
    cs = entry_scenario([(90, 92, 89, 91)])
    t = _only_trade(cs)
    assert (t.exit_time, t.exit_reason, t.exit_price) == (cs[ENTRY_BAR + 1].timestamp, ExitReason.STOP_LOSS, 90)


def test_backtest_gap_up_through_tp():
    cs = entry_scenario([(120, 121, 119, 120)])
    t = _only_trade(cs)
    assert (t.exit_time, t.exit_reason, t.exit_price) == (cs[ENTRY_BAR + 1].timestamp, ExitReason.TAKE_PROFIT, 120)


def test_backtest_intrabar_tp_not_just_close():
    cs = entry_scenario([(101, 114, 100, 102)])
    t = _only_trade(cs)
    assert t.exit_reason == ExitReason.TAKE_PROFIT and t.exit_price == pytest.approx(TP)


def test_entry_bar_itself_is_not_checked():
    cs = entry_scenario([(101, 102, 100, 101)], entry_low=90)
    assert _only_trade(cs).exit_reason == ExitReason.END


def test_backtest_trailing_uses_prior_bar_high_only():
    # bar 61: low 100 is below what its OWN high would set (110*0.96 = 105.6) but above the prior level
    # (101*0.96 = 96.96) -> no exit; bar 62: low 105 < 105.6 -> TRAILING_STOP at 105.6.
    cs = entry_scenario([(101, 110, 100, 109), (108, 108.5, 105, 106)])
    t = _only_trade(cs, _trail())
    assert t.exit_time == cs[ENTRY_BAR + 2].timestamp
    assert t.exit_reason == ExitReason.TRAILING_STOP and t.exit_price == pytest.approx(105.6)


def test_backtest_trailing_strict_touch_does_not_exit():
    lvl = 110 * 0.96
    cs = entry_scenario([(101, 110, 100, 109), (108, 108.5, lvl, 108)])
    assert _only_trade(cs, _trail()).exit_reason == ExitReason.END


def test_backtest_trailing_gap_fills_at_open():
    cs = entry_scenario([(101, 110, 100, 109), (104, 104.5, 103, 104)])
    t = _only_trade(cs, _trail())
    assert t.exit_reason == ExitReason.TRAILING_STOP and t.exit_price == 104


def test_backtest_trailing_beats_tp_same_bar():
    cs = entry_scenario([(101, 110, 100, 109), (106, 120, 105, 115)])  # trail 105.6 hit and TP 113.12 hit
    t = _only_trade(cs, _trail())
    assert t.exit_reason == ExitReason.TRAILING_STOP and t.exit_price == pytest.approx(105.6)


def test_backtest_initial_trail_above_sl_wins():
    cs = entry_scenario([(101, 101.5, 96, 100)])  # prior-bar trail 96.96 > SL 94.94, both... only trail hit
    t = _only_trade(cs, _trail())
    assert t.exit_reason == ExitReason.TRAILING_STOP and t.exit_price == pytest.approx(ENTRY_PRICE * 0.96)


def test_backtest_max_loss_off_by_default_and_uses_starting_capital():
    cs = entry_scenario([(101, 101.5, 97, 100)])
    assert _only_trade(cs).exit_reason == ExitReason.END
    s = sma_entry_strategy()
    s.risk.max_loss_per_trade_pct = 1.0  # 1% of 10k starting capital = 100 USDT on 5k/101 units
    t = _only_trade(cs, s)
    qty = 5000 / ENTRY_PRICE
    assert t.exit_reason == ExitReason.MAX_LOSS
    assert t.exit_price == pytest.approx(ENTRY_PRICE - 100 / qty) and t.pnl == pytest.approx(-100)


def test_backtest_max_loss_beats_tp_same_bar():
    s = sma_entry_strategy()
    s.risk.max_loss_per_trade_pct = 1.0
    cs = entry_scenario([(101, 120, 97, 110)])
    t = _only_trade(cs, s)
    assert t.exit_reason == ExitReason.MAX_LOSS


# ---------------------------------------------------------------- Bot 6 parity fixtures (mirrored)
T0 = datetime(2026, 1, 5, tzinfo=timezone.utc)
E = SIM_WARMUP_BARS


def _series(scenario):
    out = []
    prev = 100.0 * 1.001 ** (-SIM_WARMUP_BARS - 1)
    for i in range(SIM_WARMUP_BARS + 1):
        c = 100.0 * 1.001 ** (i - SIM_WARMUP_BARS)
        out.append(
            Candle(
                timestamp=T0 + timedelta(hours=i), open=prev, high=c * 1.0002, low=prev * 0.9998, close=c, volume=1000
            )
        )
        prev = c
    for o, h, lo, c in scenario:
        out.append(Candle(timestamp=T0 + timedelta(hours=len(out)), open=o, high=h, low=lo, close=c, volume=1000))
    last = out[-1].close
    for _ in range(3):
        out.append(
            Candle(timestamp=T0 + timedelta(hours=len(out)), open=last, high=last, low=last, close=last, volume=1000)
        )
    return out


def _fixture_strategy(trailing):
    d = ManualStrategy(name="fixture", symbol="NVDAon").model_dump()
    d["entry"]["rsi_below"]["enabled"] = False
    d["entry"]["price_above_sma"].update(enabled=True, period=2)
    d["exit"].update(take_profit_pct=10.0, stop_loss_pct=5.0)
    d["exit"]["rsi_above"]["enabled"] = False
    d["exit"]["trailing_stop"] = {"enabled": trailing, "pct": 3.0}
    d["risk"]["cooldown_bars"] = 10_000
    return ManualStrategy.model_validate(d)


CASES = {
    "sl_and_tp_same_bar": (False, [(100, 111, 94, 100)], (E + 1, ExitReason.STOP_LOSS, 95.0)),
    "gap_down_through_sl": (False, [(90, 91, 89, 90)], (E + 1, ExitReason.STOP_LOSS, 90.0)),
    "gap_up_through_tp": (False, [(115, 116, 114, 115)], (E + 1, ExitReason.TAKE_PROFIT, 115.0)),
    "intrabar_tp_fills_at_target": (False, [(101, 110.5, 100.5, 105)], (E + 1, ExitReason.TAKE_PROFIT, 110.0)),
    "intrabar_sl_fills_at_stop": (False, [(99, 99.5, 94.0, 96)], (E + 1, ExitReason.STOP_LOSS, 95.0)),
    "trailing_stop_after_new_high": (
        True,
        [(100, 108, 99, 107), (107, 107.5, 104, 105)],
        (E + 2, ExitReason.TRAILING_STOP, 108 * 0.97),
    ),
    "trailing_uses_previous_bar_level": (
        True,
        [(105, 108, 104.5, 107), (107, 107, 106, 106.5), (106, 106, 103, 103.5)],
        (E + 3, ExitReason.TRAILING_STOP, 108 * 0.97),
    ),
    "trailing_gap_down_fills_at_open": (
        True,
        [(100, 108, 99, 107), (103, 103.5, 102, 103)],
        (E + 2, ExitReason.TRAILING_STOP, 103.0),
    ),
}


@pytest.mark.parametrize("case", list(CASES))
def test_bot6_parity_fixture_cases(case):
    trailing, scenario, (want_bar, want_reason, want_px) = CASES[case]
    candles = _series(scenario)
    t = ENG.backtest_manual(_fixture_strategy(trailing), candles, 10_000.0).trades[0]
    idx = {c.timestamp: i for i, c in enumerate(candles)}
    assert idx[t.entry_time] == E and t.entry_price == pytest.approx(100.0)
    assert (idx[t.exit_time], t.exit_reason) == (want_bar, want_reason)
    assert t.exit_price == pytest.approx(want_px, rel=1e-9)


@pytest.mark.parametrize("sl_pct", [0.5, 2.0, 5.0])
def test_falling_series_fires_stop_loss(sl_pct):
    """The falling-price helper (for the QA stop-loss scenario): a stop tighter than the total fall must fire."""
    cs = falling_after_entry(drop_pct_per_bar=1.0, bars=10)
    t = _only_trade(cs, sma_entry_strategy(stop_loss_pct=sl_pct, take_profit_pct=50.0))
    assert t.exit_reason == ExitReason.STOP_LOSS
    assert t.exit_price == pytest.approx(ENTRY_PRICE * (1 - sl_pct / 100))
