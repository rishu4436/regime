import pytest

from backend.contracts import (
    BacktestResult,
    ConditionLogic,
    ExitReason,
    ManualStrategy,
    Regime,
)
from backend.engine import DefaultStrategyEngine
from backend.mocks import MockExchangeClient, generate_candles

from .helpers import ENTRY_BAR, ENTRY_PRICE, START, bar, entry_scenario, flat, sma_entry_strategy

ENG = DefaultStrategyEngine()


def or_strategy(symbol="NVDAon"):
    s = ManualStrategy(name="or", symbol=symbol)
    s.entry.logic = ConditionLogic.OR
    return s


def test_deterministic_on_seeded_mock_candles():
    cs = MockExchangeClient().get_candles("TSLAx", limit=250)
    a = ENG.backtest_manual(or_strategy("TSLAx"), cs, 10_000)
    b = ENG.backtest_manual(or_strategy("TSLAx"), list(cs), 10_000)
    assert isinstance(a, BacktestResult)
    assert a.model_dump() == b.model_dump()
    assert a.num_trades > 0


@pytest.mark.parametrize("sym", ["NVDAon", "TSLAx", "AAPLB", "SPYon", "METAx"])
def test_result_invariants(sym):
    cs = MockExchangeClient().get_candles(sym, limit=250)
    res = ENG.backtest_manual(or_strategy(sym), cs, 10_000)
    assert len(res.equity_curve) == len(cs)  # one mark-to-market point per bar
    assert [p.timestamp for p in res.equity_curve] == [c.timestamp for c in cs]
    assert res.final_equity == pytest.approx(10_000 + sum(t.pnl for t in res.trades))
    assert res.equity_curve[-1].equity == pytest.approx(res.final_equity)
    assert res.total_return_pct == pytest.approx((res.final_equity / 10_000 - 1) * 100)
    assert res.num_trades == len(res.trades)
    wins = sum(t.pnl > 0 for t in res.trades)
    assert res.win_rate_pct == pytest.approx(wins / len(res.trades) * 100 if res.trades else 0)
    assert 0 <= res.max_drawdown_pct <= 100
    for t in res.trades:
        assert t.exit_reason is not None and t.exit_time >= t.entry_time
        assert t.entry_time >= cs[60].timestamp  # warm-up respected
    # one position at a time (default max_open_positions=1): trades never overlap
    for t1, t2 in zip(res.trades, res.trades[1:], strict=False):
        assert t2.entry_time > t1.exit_time


def test_cooldown_respected():
    cs = MockExchangeClient().get_candles("TSLAx", limit=250)
    s = or_strategy("TSLAx")
    s.risk.cooldown_bars = 5
    res = ENG.backtest_manual(s, cs, 10_000)
    idx = {c.timestamp: i for i, c in enumerate(cs)}
    for t1, t2 in zip(res.trades, res.trades[1:], strict=False):
        assert idx[t2.entry_time] - idx[t1.exit_time] > 5


def test_max_open_positions_allows_stacking():
    cs = generate_candles(Regime.SIDEWAYS, n=250, seed=3)
    s = sma_entry_strategy()
    s.risk.cooldown_bars = 0
    s.risk.max_open_positions = 3
    s.risk.position_size_pct = 10
    s.exit.take_profit_pct = 50
    s.exit.stop_loss_pct = 50
    res = ENG.backtest_manual(s, cs, 10_000)
    assert res.num_trades == 3 and all(t.exit_reason == ExitReason.END for t in res.trades)


def test_mark_to_market_drawdown_includes_open_positions():
    # entry at 101, then price sinks to 96 (above SL 94.94) and recovers to 101: realised-only equity would be flat.
    cs = entry_scenario([(100, 100, 96, 96), (96, 101, 96, 101)])
    res = ENG.backtest_manual(sma_entry_strategy(), cs, 10_000)
    qty = 5000 / ENTRY_PRICE
    assert res.max_drawdown_pct == pytest.approx(qty * 5 / 10_000 * 100)
    assert res.equity_curve[ENTRY_BAR + 1].equity == pytest.approx(10_000 - qty * 5)


def test_fees_and_slippage():
    cs = entry_scenario([(120, 121, 119, 120)])
    s = sma_entry_strategy()
    res = ENG.backtest_manual(s, cs, 10_000, fee_pct=0.1, slippage_bps=10)
    t = res.trades[0]
    buy = ENTRY_PRICE * 1.001
    notional = 5000.0
    qty = notional / buy
    sell = 120 * 0.999
    proceeds = qty * sell
    pnl = proceeds * 0.999 - notional * 1.001
    assert t.entry_price == pytest.approx(buy) and t.exit_price == pytest.approx(sell)
    assert t.quantity == pytest.approx(qty) and t.pnl == pytest.approx(pnl)
    assert t.pnl_pct == pytest.approx(pnl / (notional * 1.001) * 100)
    no_cost = ENG.backtest_manual(s, cs, 10_000)
    assert res.final_equity < no_cost.final_equity
    eng = DefaultStrategyEngine(fee_pct=0.1, slippage_bps=10)  # engine-level defaults
    assert eng.backtest_manual(s, cs, 10_000).final_equity == pytest.approx(res.final_equity)


def test_end_close_and_no_entry_on_last_bar():
    cs = flat(61) + [bar(61, 100, 101, 100, 101)]  # only the very last bar satisfies close > SMA(2)
    res = ENG.backtest_manual(sma_entry_strategy(), cs, 10_000)
    assert res.num_trades == 0
    cs = entry_scenario([(101, 102, 100, 102)])
    t = ENG.backtest_manual(sma_entry_strategy(), cs, 10_000).trades[0]
    assert t.exit_reason == ExitReason.END and t.exit_price == 102 and t.exit_time == cs[-1].timestamp


def test_min_volume_is_hard_filter_not_trigger_under_or():
    cs = flat(80, v=1000.0)
    cs[70] = bar(70, 100, 100, 100, 100, v=10_000.0)  # volume spike, nothing else happens
    s = ManualStrategy(name="mv")
    s.entry.logic = ConditionLogic.OR
    s.entry.price_above_sma.enabled = False
    s.entry.rsi_below.threshold = 0  # never true
    s.filters.min_volume.enabled = True
    assert ENG.backtest_manual(s, cs, 10_000).num_trades == 0  # simulator would have entered at bar 70
    s.entry.volume_above_avg.enabled = True  # as an ENTRY condition it may trigger
    res = ENG.backtest_manual(s, cs, 10_000)
    assert res.num_trades == 1 and res.trades[0].entry_time == cs[70].timestamp


def test_market_hours_filter_uses_candle_timestamps():
    cs = generate_candles(Regime.SIDEWAYS, n=400, seed=9)
    s = or_strategy()
    s.risk.cooldown_bars = 0
    s.filters.market_hours.enabled = True
    res = ENG.backtest_manual(s, cs, 10_000)
    assert res.num_trades > 0
    for t in res.trades:
        assert 13 <= t.entry_time.hour < 20 and t.entry_time.weekday() < 5


def test_and_or_logic_difference():
    cs = MockExchangeClient().get_candles("TSLAx", limit=250)
    and_res = ENG.backtest_manual(ManualStrategy(symbol="TSLAx"), cs, 10_000)
    or_res = ENG.backtest_manual(or_strategy("TSLAx"), cs, 10_000)
    assert or_res.num_trades >= and_res.num_trades


def test_edge_cases():
    empty = ENG.backtest_manual(ManualStrategy(), [], 1000)
    assert empty.final_equity == 1000 and empty.num_trades == 0 and empty.equity_curve == []
    short = ENG.backtest_manual(ManualStrategy(), flat(30), 1000)
    assert short.num_trades == 0 and len(short.equity_curve) == 30
    with pytest.raises(ValueError):
        ENG.backtest_manual(ManualStrategy(), flat(10), 0)
    with pytest.raises(ValueError):
        ENG.backtest_manual(ManualStrategy(), flat(10), 100, fee_pct=-1)


def test_unsorted_candles_are_sorted():
    cs = MockExchangeClient().get_candles("TSLAx", limit=250)
    a = ENG.backtest_manual(or_strategy("TSLAx"), cs, 10_000)
    b = ENG.backtest_manual(or_strategy("TSLAx"), list(reversed(cs)), 10_000)
    assert a.model_dump() == b.model_dump()


def test_detailed_notes_align_with_trades():
    cs = MockExchangeClient().get_candles("TSLAx", limit=250)
    run = ENG.backtest_manual_detailed(or_strategy("TSLAx"), cs, 10_000)
    assert len(run.notes) == len(run.result.trades)
    assert all(n.entry_reason.startswith("OR:") and n.exit_detail for n in run.notes)


def test_start_constant_is_monday():
    assert START.weekday() == 0


def test_backtest_entries_match_live_evaluator_on_prefix():
    """Same code path: every backtest entry bar is a BUY for evaluate_manual on the candles up to that bar."""
    from backend.contracts import Action, Portfolio

    cs = MockExchangeClient().get_candles("TSLAx", limit=250)
    s = or_strategy("TSLAx")
    res = ENG.backtest_manual(s, cs, 10_000)
    idx = {c.timestamp: i for i, c in enumerate(cs)}
    empty = Portfolio(starting_capital=10_000, cash=10_000, equity=10_000)
    for t in res.trades:
        i = idx[t.entry_time]
        assert ENG.evaluate_manual(s, cs[: i + 1], empty).action == Action.BUY
