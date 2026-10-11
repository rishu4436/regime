"""Auto sizing on live uses the real portfolio equity, not only the allocated AutoConfig.capital."""

from __future__ import annotations

import pytest

from backend.contracts import Action, AutoConfig, Portfolio
from backend.engine import DefaultStrategyEngine

from .helpers import bar


def wiggle(n: int = 120, price: float = 100.0, amp: float = 0.2, v: float = 1000.0) -> list:
    cs = []
    for i in range(n):
        c = price * (1 + (amp / 100) * (1 if i % 2 else -1))
        cs.append(bar(i, price, max(price, c) * 1.001, min(price, c) * 0.999, c, v))
    return cs


def test_auto_live_size_uses_real_equity_not_allocated_capital():
    """Rishu's PC: AUTO/GRID with a large AutoConfig.capital on a ~110 USDT wallet proposed 25 USDT (2% of the
    allocation, hitting the 25 cap). The basis must be min(capital, equity): 2% of 109.87 -> raised to the 10 USDT
    NVDAB minimum."""
    from datetime import timedelta

    cs = wiggle(n=120)
    cs.append(bar(120, 99.8, 99.8, 99.74, 99.75, 1000.0))  # bottom grid cell, still inside the range
    now = cs[-1].timestamp + timedelta(minutes=30)
    port = Portfolio(starting_capital=109.87, cash=99.87, equity=109.87)
    eng = DefaultStrategyEngine(live_safe=True, clock=lambda: now)
    eng.set_market_context("NVDAB", {"status": "trading", "min_order_usdt": 10})
    sig = eng.evaluate_auto(AutoConfig(symbol="NVDAB", capital=10_000), cs, port)
    assert sig.action == Action.BUY and "bottom grid" in sig.reason, sig.reason
    assert sig.proposed_notional == pytest.approx(10.0), sig.reason
    big = Portfolio(starting_capital=10_000, cash=10_000, equity=10_000)
    assert eng.evaluate_auto(AutoConfig(symbol="NVDAB", capital=10_000), cs, big).proposed_notional == pytest.approx(25.0)
    assert DefaultStrategyEngine._auto_basis(AutoConfig(symbol="X", capital=50), port) == 50
