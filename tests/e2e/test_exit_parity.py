"""Exit parity: Engine.backtest_manual vs the live loop's RiskManager.check_exits path must agree on
exit bar, exit reason and fill price (agreed rule, see backend/app/exits.py):
- trailing stop checked against the bar LOW using the PREVIOUS bar's level, then raised with this bar's HIGH;
- exits fill at the stop / target price, or at the OPEN when the bar gaps through the level (SL gap down, TP gap up);
- SL and TP touched in the same bar -> STOP_LOSS (pessimistic; documented assumption, confirm with Bot 2/3).
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


def _fixture_strategy(trailing: bool) -> ManualStrategy:
    s = ManualStrategy(name="fixture", symbol="NVDAon")
    d = s.model_dump()
    for k in ("rsi_below", "price_above_sma"):
        d["entry"][k]["enabled"] = False
    d["entry"]["price_above_sma"].update(enabled=True, period=2)
    d["exit"].update(take_profit_pct=10.0, stop_loss_pct=5.0)
    d["exit"]["rsi_above"]["enabled"] = False
    d["exit"]["trailing_stop"] = {"enabled": trailing, "pct": 3.0}
    d["risk"]["cooldown_bars"] = 10_000  # exactly one trade
    return ManualStrategy.model_validate(d)


E = SIM_WARMUP_BARS  # entry bar
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
}


@pytest.mark.parametrize("engine,risk", engine_risk_impls())
@pytest.mark.parametrize("case", list(CASES))
def test_exit_fixture_cases(engine, risk, case):
    trailing, scenario, (want_bar, want_reason, want_px) = CASES[case]
    candles = _series(scenario)
    strat = _fixture_strategy(trailing)
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
