from datetime import datetime, timedelta, timezone

import pytest

from backend.contracts import Action, AgentMode, ControlState, Portfolio, Position, Signal

T0 = datetime(2026, 1, 5, 14, 0, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


@pytest.fixture
def clock():
    return FakeClock()


def make_portfolio(cash=10000.0, positions=(), equity=None, **kw):
    positions = list(positions)
    if equity is None:
        equity = cash + sum(p.quantity * (p.current_price or p.avg_entry_price) for p in positions)
    return Portfolio(starting_capital=kw.pop("starting_capital", 10000.0), cash=cash, positions=positions,
                     equity=equity, **kw)


def pos(symbol="NVDAon", qty=10.0, entry=100.0, strategy="MANUAL:test", **kw):
    return Position(symbol=symbol, quantity=qty, avg_entry_price=entry, opened_at=T0, strategy=strategy, **kw)


def buy(symbol="NVDAon", **kw):
    return Signal(symbol=symbol, action=Action.BUY, source=AgentMode.MANUAL, strategy="MANUAL:test",
                  reason="rsi<35 and close>sma50", **kw)


def sell(symbol="NVDAon", **kw):
    return Signal(symbol=symbol, action=Action.SELL, source=AgentMode.MANUAL, strategy="MANUAL:test",
                  reason="RSI exit", **kw)


def ctl(**kw):
    kw.setdefault("confirmation_mode", False)
    return ControlState(**kw)
