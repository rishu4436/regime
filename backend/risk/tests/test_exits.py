"""Exit semantics agreed with Bot 2's backtest_manual (see backend/risk/exits.py)."""
import pytest

from backend.contracts import ExitReason, ExitRules, TrailingStopRule
from backend.risk.exits import Bar, compute_levels, evaluate_bar, max_loss_level

from .conftest import pos

RULES = ExitRules()  # TP 12, SL 6, trailing off
TRAIL = ExitRules(trailing_stop=TrailingStopRule(enabled=True, pct=4.0))


def lv(rules=RULES, hwm=100.0, max_loss=None, **pkw):
    return compute_levels(pos(**pkw), rules, hwm, max_loss)


def test_default_levels_match_simulator():
    l = lv()
    assert l.stop_loss == pytest.approx(94.0) and l.take_profit == pytest.approx(112.0)
    assert l.trailing_stop is None
    assert lv(TRAIL).trailing_stop == pytest.approx(96.0)  # first trail = entry * (1 - 4%)


def test_position_overrides_levels():
    l = lv(stop_loss_price=90.0, take_profit_price=130.0, trailing_stop_pct=10.0, hwm=110.0)
    assert (l.stop_loss, l.take_profit, l.trailing_stop) == (90.0, 130.0, pytest.approx(99.0))


def test_no_exit_inside_band_raises_hwm():
    d, hwm = evaluate_bar(lv(TRAIL), Bar(100, 105, 97, 104))
    assert d is None and hwm == 105


def test_stop_loss_on_intrabar_low_fills_at_level():
    d, _ = evaluate_bar(lv(), Bar(100, 101, 93, 99))  # close back above stop: close-only sim would NOT exit
    assert d.reason == ExitReason.STOP_LOSS and d.fill_price == pytest.approx(94.0) and not d.gapped
    assert d.pnl_pct == pytest.approx(-6.0)


def test_stop_loss_touch_exactly():
    d, _ = evaluate_bar(lv(), Bar(95, 95, 94.0, 95))
    assert d.reason == ExitReason.STOP_LOSS


def test_take_profit_on_intrabar_high_fills_at_level():
    d, _ = evaluate_bar(lv(), Bar(110, 113, 109, 111))
    assert d.reason == ExitReason.TAKE_PROFIT and d.fill_price == pytest.approx(112.0) and not d.gapped


def test_tp_and_sl_same_bar_sl_wins():
    d, _ = evaluate_bar(lv(), Bar(100, 115, 90, 110))
    assert d.reason == ExitReason.STOP_LOSS and d.fill_price == pytest.approx(94.0)
    assert "stop wins" in d.detail


def test_gap_down_through_stop_fills_at_open():
    d, _ = evaluate_bar(lv(), Bar(90, 92, 88, 91))
    assert d.reason == ExitReason.STOP_LOSS and d.fill_price == 90 and d.gapped and "gapped" in d.detail


def test_gap_up_through_target_fills_at_open():
    d, _ = evaluate_bar(lv(), Bar(115, 118, 114, 116))
    assert d.reason == ExitReason.TAKE_PROFIT and d.fill_price == 115 and d.gapped


def test_trailing_checks_previous_trail_before_raising():
    # hwm before bar = 105 -> trail 100.8. Bar makes new high 120 then dips to 101: NOT an exit,
    # because the low is checked against the previous trail (100.8) before raising it with this bar's high.
    l = lv(TRAIL, hwm=105.0)
    d, hwm = evaluate_bar(l, Bar(106, 111, 101, 110))
    assert d is None and hwm == 111
    # Next bar: trail = 111 * 0.96 = 106.56; low 106 -> trailing stop at 106.56
    d, _ = evaluate_bar(lv(TRAIL, hwm=hwm), Bar(109, 109.5, 106, 107))
    assert d.reason == ExitReason.TRAILING_STOP and d.fill_price == pytest.approx(106.56)


def test_trailing_is_strict_like_simulator():
    d, _ = evaluate_bar(lv(TRAIL, hwm=110.0), Bar(106, 107, 105.6, 106))  # low == trail 105.6 -> no exit
    assert d is None


def test_trailing_gap_fills_at_open():
    d, _ = evaluate_bar(lv(TRAIL, hwm=110.0), Bar(103, 104, 102, 103))
    assert d.reason == ExitReason.TRAILING_STOP and d.fill_price == 103 and d.gapped


def test_highest_stop_wins_when_several_hit():
    # trail (hwm 110 -> 105.6) above SL 94 -> trailing stop reported/filled
    d, _ = evaluate_bar(lv(TRAIL, hwm=110.0), Bar(106, 107, 90, 92))
    assert d.reason == ExitReason.TRAILING_STOP and d.fill_price == pytest.approx(105.6)
    # trail below SL (hwm 97.5 cannot happen in practice, but ordering is by level)
    d, _ = evaluate_bar(lv(TRAIL, hwm=100.0, stop_loss_price=97.0), Bar(98, 98, 90, 92))
    assert d.reason == ExitReason.STOP_LOSS and d.fill_price == 97.0


def test_trailing_stop_beats_take_profit_same_bar():
    d, _ = evaluate_bar(lv(TRAIL, hwm=111.0), Bar(111, 113, 105, 112))
    assert d.reason == ExitReason.TRAILING_STOP


def test_price_only_bar_reduces_to_close_check():
    d, _ = evaluate_bar(lv(), Bar.from_price(93.0))
    assert d.reason == ExitReason.STOP_LOSS and d.fill_price == 93.0
    d, _ = evaluate_bar(lv(), Bar.from_price(112.5))
    assert d.reason == ExitReason.TAKE_PROFIT and d.fill_price == 112.5
    d, hwm = evaluate_bar(lv(TRAIL), Bar.from_price(104.0))
    assert d is None and hwm == 104.0


def test_max_loss_level_and_exit():
    p = pos(qty=10, entry=100)
    assert max_loss_level(p, None, 10000) is None
    lvl = max_loss_level(p, 0.5, 10000)  # 50 USDT over 10 units -> 5 below entry
    assert lvl == pytest.approx(95.0)
    assert max_loss_level(p, 50, 10000) is None  # unreachable (<= 0)
    d, _ = evaluate_bar(lv(max_loss=lvl), Bar(99, 99, 94.5, 95))
    assert d.reason == ExitReason.MAX_LOSS and d.fill_price == pytest.approx(95.0)
