"""Exit parity: Engine.backtest_manual vs the live loop's RiskManager.check_exits path must agree on
exit bar, exit reason and fill price (agreed rule, see backend/app/exits.py):
- trailing stop: `low < level` (strict) with the level from PRIOR bars' highs; the app raises highest_price after;
- any stop (SL / trailing / max-loss) beats a take-profit touched in the same bar; several stops -> highest level wins;
- fills at the stop / target level, or at the OPEN when the bar gaps through it (SL gap down, TP gap up);
- emergency stop -> EMERGENCY_STOP exits at the bar close; check_exits never mutates the portfolio.
Runs for the app fallbacks always and for the real Bot 2 + Bot 3 classes once both are implemented.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.app.loop import call_check_exits
from backend.app.portfolio import PortfolioBook
from backend.contracts import (
    Action, Candle, ExecutionPath, ExecutionResult, ExitReason, ManualStrategy, OrderSide, OrderStatus, Regime,
    SIM_WARMUP_BARS, TradingMode,
)
from backend.mocks import generate_candles

from .helpers import engine_risk_impls

PROTECTIVE = {ExitReason.STOP_LOSS, ExitReason.TAKE_PROFIT, ExitReason.TRAILING_STOP}


def live_exit(risk, strategy: ManualStrategy, candles: list[Candle], entry_idx: int, entry_price: float, qty: float):
    """Open one lot in the app's PortfolioBook exactly like the loop does, then feed bars through the loop's
    check_exits path (call_check_exits -> raise_highs). Returns (bar_index, ExitReason, price) or None."""
    book = PortfolioBook(10_000.0, TradingMode.SIM)
    sym = strategy.symbol
    fill = ExecutionResult(client_order_id="t", symbol=sym, side=OrderSide.BUY, status=OrderStatus.FILLED,
                           path=ExecutionPath.SWAP, mode=TradingMode.SIM, filled_quantity=qty, avg_price=entry_price,
                           notional=qty * entry_price)
    hook = getattr(risk, "apply_manual_strategy", None)  # same optional hook the loop calls on activation
    if callable(hook):
        hook(strategy)
    pos = book.open_lot(fill, qty * entry_price, f"MANUAL:{strategy.name}", strategy.exit, candles[entry_idx].timestamp)
    for j in range(entry_idx + 1, len(candles)):
        bar = candles[j]
        book.on_bar(sym, bar, bar.timestamp)
        sigs = [s for s in call_check_exits(risk, book.portfolio(), {sym: bar.close}, {sym: bar}) if s.action == Action.SELL]
        if sigs:
            s = sigs[0]
            assert s.exit_reason is not None, "check_exits SELL must carry Signal.exit_reason (contract v0.2)"
            assert s.position_id in (None, pos.id), "Signal.position_id must target the open Position"
            return j, s.exit_reason, s.price
        book.raise_highs(sym, bar.high)
    return None


def _strategy(**exit_kw) -> ManualStrategy:
    s = ManualStrategy(name="parity", symbol="NVDAon")
    d = s.model_dump()
    d["entry"]["logic"] = "OR"
    d["entry"]["rsi_below"].update(enabled=True, threshold=45)
    d["entry"]["price_above_sma"]["enabled"] = False
    d["entry"]["bollinger_lower"]["enabled"] = True
    d["exit"]["rsi_above"]["enabled"] = False  # close-based exits are engine-only; parity is about protective exits
    d["exit"].update(exit_kw)
    d["risk"]["cooldown_bars"] = 1
    return ManualStrategy.model_validate(d)


@pytest.mark.parametrize("engine,risk", engine_risk_impls())
@pytest.mark.parametrize("regime,seed", [(Regime.HIGH_VOLATILITY, 1), (Regime.SIDEWAYS, 2), (Regime.MILD_UPTREND, 3), (Regime.DOWNTREND, 4)])
@pytest.mark.parametrize("trailing", [False, True], ids=["no-trail", "trail"])
def test_backtest_and_live_exits_agree_on_seeded_candles(engine, risk, regime, seed, trailing):
    candles = generate_candles(regime, n=400, seed=seed, start_price=140.0)
    strat = _strategy(take_profit_pct=3.0, stop_loss_pct=2.0,
                      trailing_stop={"enabled": trailing, "pct": 1.5})
    bt = engine.backtest_manual(strat, candles, 10_000.0)
    idx = {c.timestamp: i for i, c in enumerate(candles)}
    assert bt.trades, "parity needs trades - fixture strategy should trade on every regime"
    checked = 0
    for t in bt.trades:
        e, x = idx[t.entry_time], idx[t.exit_time]
        got = live_exit(risk, strat, candles, e, t.entry_price, t.quantity)
        if t.exit_reason in PROTECTIVE:
            assert got is not None, f"live path never exited trade entered at bar {e} (backtest: {t.exit_reason} @ bar {x})"
            assert (got[0], got[1]) == (x, t.exit_reason), f"entry bar {e}"
            assert got[2] == pytest.approx(t.exit_price, rel=1e-9)
            checked += 1
        else:  # END / RSI / MACD: no protective exit may fire up to and including the backtest exit bar
            assert got is None or got[0] > x, f"live path exited at bar {got[0]} ({got[1]}) before backtest {t.exit_reason} @ {x}"
    assert checked > 0


# ------------------------------------------------------------------ hand-made fixture bars
T0 = datetime(2026, 1, 5, tzinfo=timezone.utc)  # Monday


def _series(scenario: list[tuple[float, float, float, float]]) -> list[Candle]:
    """61 slowly rising bars so a 'close > SMA(2)' rule enters at bar 60 at exactly 100.0, then the scenario bars,
    then flat bars."""
    out: list[Candle] = []
    prev = 100.0 * 1.001 ** (-SIM_WARMUP_BARS - 1)
    for i in range(SIM_WARMUP_BARS + 1):
        c = 100.0 * 1.001 ** (i - SIM_WARMUP_BARS)
        out.append(Candle(timestamp=T0 + timedelta(hours=i), open=prev, high=c * 1.0002, low=prev * 0.9998, close=c, volume=1000))
        prev = c
    for o, h, l, c in scenario:
        out.append(Candle(timestamp=T0 + timedelta(hours=len(out)), open=o, high=h, low=l, close=c, volume=1000))
    last = out[-1].close
    for _ in range(3):
        out.append(Candle(timestamp=T0 + timedelta(hours=len(out)), open=last, high=last, low=last, close=last, volume=1000))
    return out


def _fixture_strategy(trailing: bool, max_loss_pct=None) -> ManualStrategy:
    s = ManualStrategy(name="fixture", symbol="NVDAon")
    d = s.model_dump()
    for k in ("rsi_below", "price_above_sma"):
        d["entry"][k]["enabled"] = False
    d["entry"]["price_above_sma"].update(enabled=True, period=2)
    d["exit"].update(take_profit_pct=10.0, stop_loss_pct=5.0)
    d["exit"]["rsi_above"]["enabled"] = False
    d["exit"]["trailing_stop"] = {"enabled": trailing, "pct": 3.0}
    d["risk"]["cooldown_bars"] = 10_000  # exactly one trade
    d["risk"]["max_loss_per_trade_pct"] = max_loss_pct  # 15% size -> 15 units at 100; 0.3% of 10k = 30 USDT -> level 98
    return ManualStrategy.model_validate(d)


E = SIM_WARMUP_BARS  # entry bar
TRAIL_108 = 108 * (1 - 3.0 / 100)  # trailing level after a 108 high (same float expression the implementations use)
CASES = {
    # name: (trailing, scenario bars, expected (bar index, reason, fill price))
    "sl_and_tp_same_bar": (False, [(100, 111, 94, 100)], (E + 1, ExitReason.STOP_LOSS, 95.0)),
    "gap_down_through_sl": (False, [(90, 91, 89, 90)], (E + 1, ExitReason.STOP_LOSS, 90.0)),
    "gap_up_through_tp": (False, [(115, 116, 114, 115)], (E + 1, ExitReason.TAKE_PROFIT, 115.0)),
    "intrabar_tp_fills_at_target": (False, [(101, 110.5, 100.5, 105)], (E + 1, ExitReason.TAKE_PROFIT, 110.0)),
    "intrabar_sl_fills_at_stop": (False, [(99, 99.5, 94.0, 96)], (E + 1, ExitReason.STOP_LOSS, 95.0)),
    "trailing_stop_after_new_high": (True, [(100, 108, 99, 107), (107, 107.5, 104, 105)], (E + 2, ExitReason.TRAILING_STOP, 108 * 0.97)),
    # bar E+1 low 104.5 is below 108*0.97 (this bar's high) but above the previous level 97 -> no exit on E+1
    "trailing_uses_previous_bar_level": (True, [(105, 108, 104.5, 107), (107, 107, 106, 106.5), (106, 106, 103, 103.5)],
                                         (E + 3, ExitReason.TRAILING_STOP, 108 * 0.97)),
    "trailing_gap_down_fills_at_open": (True, [(100, 108, 99, 107), (103, 103.5, 102, 103)], (E + 2, ExitReason.TRAILING_STOP, 103.0)),
    # Bot 3 edge rules
    "gap_up_through_tp_then_sl_same_bar_stop_wins": (False, [(115, 116, 94, 100)], (E + 1, ExitReason.STOP_LOSS, 95.0)),
    "trailing_beats_tp_same_bar": (True, [(100, 108, 99, 107), (106, 111, 104, 105)], (E + 2, ExitReason.TRAILING_STOP, TRAIL_108)),
    "several_stops_highest_level_wins": (True, [(99, 99.5, 94, 96)], (E + 1, ExitReason.TRAILING_STOP, 97.0)),
    "trailing_is_strict_low_equal_level_no_exit": (True, [(100, 108, 99, 107), (106, 107, TRAIL_108, 105), (105, 105, 104, 104.5)],
                                                   (E + 3, ExitReason.TRAILING_STOP, TRAIL_108)),
    "max_loss_beats_tp_same_bar": (False, [(100, 111, 97, 100)], (E + 1, ExitReason.MAX_LOSS, 98.0), 0.3),
    "max_loss_above_sl_wins": (False, [(99, 99, 94, 96)], (E + 1, ExitReason.MAX_LOSS, 98.0), 0.3),
    "sl_above_max_loss_wins": (False, [(99, 99, 89, 96)], (E + 1, ExitReason.STOP_LOSS, 95.0), 1.5),  # max-loss level 100-150/15=90
}


@pytest.mark.parametrize("engine,risk", engine_risk_impls())
@pytest.mark.parametrize("case", list(CASES))
def test_exit_fixture_cases(engine, risk, case):
    trailing, scenario, (want_bar, want_reason, want_px), *rest = CASES[case]
    candles = _series(scenario)
    strat = _fixture_strategy(trailing, rest[0] if rest else None)
    bt = engine.backtest_manual(strat, candles, 10_000.0)
    assert bt.trades, "fixture should enter at bar 60"
    t = bt.trades[0]
    idx = {c.timestamp: i for i, c in enumerate(candles)}
    assert idx[t.entry_time] == E and t.entry_price == pytest.approx(100.0)
    assert (idx[t.exit_time], t.exit_reason) == (want_bar, want_reason)
    assert t.exit_price == pytest.approx(want_px, rel=1e-9)
    got = live_exit(risk, strat, candles, E, t.entry_price, t.quantity)
    assert got is not None
    assert (got[0], got[1]) == (want_bar, want_reason)
    assert got[2] == pytest.approx(want_px, rel=1e-9)


# ------------------------------------------------------------------ risk-only rules
def _one_lot_portfolio():
    from backend.contracts import Portfolio, Position
    pos = Position(symbol="NVDAon", quantity=10, avg_entry_price=100, strategy="MANUAL:x", highest_price=104,
                   stop_loss_price=95, take_profit_price=110, trailing_stop_pct=3.0, current_price=101)
    return Portfolio(starting_capital=10_000, cash=9_000, positions=[pos], equity=10_010)


def _bar(o, h, l, c):
    return Candle(timestamp=T0, open=o, high=h, low=l, close=c, volume=1000)


@pytest.mark.parametrize("engine,risk", engine_risk_impls())
def test_check_exits_never_mutates_portfolio(engine, risk):
    pf = _one_lot_portfolio()
    before = pf.model_dump()
    for bar in (_bar(101, 109, 100.5, 108), _bar(100, 100, 90, 92), _bar(101, 112, 100.5, 111)):
        call_check_exits(risk, pf, {"NVDAon": bar.close}, {"NVDAon": bar})
        assert pf.model_dump() == before


@pytest.mark.parametrize("engine,risk", engine_risk_impls())
def test_emergency_stop_exits_fill_at_close(engine, risk):
    pf = _one_lot_portfolio()
    risk.emergency_stop("test")
    try:
        bar = _bar(101, 103, 100.5, 102)  # no SL/TP/trailing level touched
        sigs = call_check_exits(risk, pf, {"NVDAon": bar.close}, {"NVDAon": bar})
        assert len(sigs) == 1 and sigs[0].action == Action.SELL
        assert sigs[0].exit_reason == ExitReason.EMERGENCY_STOP and sigs[0].price == pytest.approx(102)
        assert sigs[0].position_id == pf.positions[0].id
        # e-stop blocks BUYs but allows the closing SELL
        from backend.contracts import AgentMode, Signal
        buy = Signal(symbol="NVDAon", action=Action.BUY, source=AgentMode.MANUAL, strategy="x", reason="x", proposed_notional=100, price=102)
        assert not risk.check(buy, pf, risk.get_control_state()).approved
        assert risk.check(sigs[0], pf, risk.get_control_state()).approved
    finally:
        risk.reset_emergency_stop()
