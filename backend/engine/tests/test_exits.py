"""Exit semantics agreed with Bot 3 (check_exits). Bot 6 runs a parity test against these exact rules."""

import pytest

from backend.contracts import ExitReason
from backend.engine import DefaultStrategyEngine
from backend.engine.exits import check_price_exit, initial_trail, max_loss_level, ratchet_trail, stop_level

from .helpers import ENTRY_BAR, ENTRY_PRICE, entry_scenario, sma_entry_strategy

ENG = DefaultStrategyEngine()
TP = ENTRY_PRICE * 1.12  # 113.12
SL = ENTRY_PRICE * 0.94  # 94.94


# ---------------------------------------------------------------- unit level
def test_same_bar_sl_and_tp_sl_wins():
    pe = check_price_exit(101, 115, 94, 101, 12, 6)
    assert pe.reason == ExitReason.STOP_LOSS and pe.price == pytest.approx(SL) and not pe.gap


def test_gap_down_through_stop_fills_at_open():
    pe = check_price_exit(90, 92, 89, 101, 12, 6)
    assert pe.reason == ExitReason.STOP_LOSS and pe.price == 90 and pe.gap


def test_gap_up_through_tp_fills_at_open():
    pe = check_price_exit(120, 121, 119, 101, 12, 6)
    assert pe.reason == ExitReason.TAKE_PROFIT and pe.price == 120 and pe.gap


def test_open_exactly_at_levels_counts_as_gap():
    assert check_price_exit(SL, 100, 90, 101, 12, 6).price == pytest.approx(SL)
    assert check_price_exit(TP, 120, 110, 101, 12, 6).reason == ExitReason.TAKE_PROFIT


def test_tp_only_fills_at_level_and_no_exit_inside_range():
    pe = check_price_exit(101, 114, 100, 101, 12, 6)
    assert pe.reason == ExitReason.TAKE_PROFIT and pe.price == pytest.approx(TP)
    assert check_price_exit(101, 113, 95, 101, 12, 6) is None


def test_stop_priority_and_ties():
    assert stop_level(100, 6, 96.0) == (96.0, ExitReason.TRAILING_STOP)
    assert stop_level(100, 6, 93.0) == (pytest.approx(94.0), ExitReason.STOP_LOSS)
    assert stop_level(100, 4, 96.0)[1] == ExitReason.TRAILING_STOP  # exact tie -> trailing
    assert stop_level(100, 6, None, 97.0) == (97.0, ExitReason.MAX_LOSS)
    assert stop_level(100, 3, None, 97.0)[1] == ExitReason.STOP_LOSS  # tie SL vs MAX_LOSS -> SL


def test_trail_helpers():
    assert initial_trail(100, 4) == 96
    assert ratchet_trail(96, 110, 4) == pytest.approx(105.6)
    assert ratchet_trail(105.6, 100, 4) == 105.6  # never moves down
    assert max_loss_level(100, 10, 1000, 2) == pytest.approx(98)  # lose 20 USDT on 10 units
    assert max_loss_level(100, 10, 1000, None) is None


# ---------------------------------------------------------------- inside backtest_manual
def _only_trade(cs, strategy=None):
    res = ENG.backtest_manual(strategy or sma_entry_strategy(), cs, 10_000)
    assert res.num_trades == 1, [t.exit_reason for t in res.trades]
    t = res.trades[0]
    assert t.entry_time == cs[ENTRY_BAR].timestamp and t.entry_price == ENTRY_PRICE
    return t


def test_backtest_same_bar_sl_tp():
    cs = entry_scenario([(101, 115, 94, 110)])
    t = _only_trade(cs)
    assert (t.exit_time, t.exit_reason, t.exit_price) == (
        cs[ENTRY_BAR + 1].timestamp,
        ExitReason.STOP_LOSS,
        pytest.approx(SL),
    )


def test_backtest_gap_down_through_sl():
    cs = entry_scenario([(90, 92, 89, 91)])
    t = _only_trade(cs)
    assert (t.exit_time, t.exit_reason, t.exit_price) == (cs[ENTRY_BAR + 1].timestamp, ExitReason.STOP_LOSS, 90)


def test_backtest_gap_up_through_tp():
    cs = entry_scenario([(120, 121, 119, 120)])
    t = _only_trade(cs)
    assert (t.exit_time, t.exit_reason, t.exit_price) == (cs[ENTRY_BAR + 1].timestamp, ExitReason.TAKE_PROFIT, 120)


def test_backtest_intrabar_tp_not_just_close():
    cs = entry_scenario([(101, 114, 100, 102)])  # close never reaches TP, high does
    t = _only_trade(cs)
    assert t.exit_reason == ExitReason.TAKE_PROFIT and t.exit_price == pytest.approx(TP)


def test_entry_bar_itself_is_not_checked():
    cs = entry_scenario([(101, 102, 100, 101)], entry_low=90)  # entry bar low is below SL
    t = _only_trade(cs)
    assert t.exit_reason == ExitReason.END


def test_backtest_trailing_carried_then_ratcheted():
    from backend.contracts import TrailingStopRule

    s = sma_entry_strategy(trailing_stop=TrailingStopRule(enabled=True, pct=4))
    # bar 61: low 100 is below the level the HIGH 110 would set (105.6) but above the carried 96.96 -> no exit;
    # the trail ratchets to 105.6 afterwards. bar 62: low 105 <= 105.6 -> TRAILING_STOP at 105.6.
    cs = entry_scenario([(101, 110, 100, 109), (108, 108.5, 105, 106)])
    t = _only_trade(cs, s)
    assert t.exit_time == cs[ENTRY_BAR + 2].timestamp
    assert t.exit_reason == ExitReason.TRAILING_STOP and t.exit_price == pytest.approx(105.6)


def test_backtest_trailing_gap_fills_at_open():
    from backend.contracts import TrailingStopRule

    s = sma_entry_strategy(trailing_stop=TrailingStopRule(enabled=True, pct=4))
    cs = entry_scenario([(101, 110, 100, 109), (104, 104.5, 103, 104)])
    t = _only_trade(cs, s)
    assert t.exit_reason == ExitReason.TRAILING_STOP and t.exit_price == 104


def test_backtest_initial_trail_beats_sl():
    from backend.contracts import TrailingStopRule

    s = sma_entry_strategy(trailing_stop=TrailingStopRule(enabled=True, pct=4))
    cs = entry_scenario([(101, 101.5, 96, 100)])  # carried trail 96.96 > SL 94.94
    t = _only_trade(cs, s)
    assert t.exit_reason == ExitReason.TRAILING_STOP and t.exit_price == pytest.approx(ENTRY_PRICE * 0.96)


def test_backtest_max_loss_per_trade_off_by_default_and_works_when_set():
    cs = entry_scenario([(101, 101.5, 97, 100)])
    assert _only_trade(cs).exit_reason == ExitReason.END  # default: no max-loss rule
    s = sma_entry_strategy()
    s.risk.max_loss_per_trade_pct = 1.0  # 1% of 10k = 100 USDT on a 5k position (49.5 units) -> level ~98.98
    t = _only_trade(cs, s)
    assert t.exit_reason == ExitReason.MAX_LOSS
    qty = 5000 / ENTRY_PRICE
    assert t.exit_price == pytest.approx(ENTRY_PRICE - 100 / qty)
    assert t.pnl == pytest.approx(-100)
