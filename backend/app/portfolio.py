"""PortfolioBook: the app's single source of truth for cash, open lots and PnL (thread-safe via the loop lock).

Each entry is its own lot (`Position`), mirroring the simulator's per-position SL/TP/trailing. The book also
maintains `Position.highest_price` (the trailing high-water mark): it is raised with the bar HIGH *after*
`RiskManager.check_exits` ran for that bar, so check_exits always sees the previous bar's trailing level
(agreed exit rule, see exits.py).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

from backend.contracts import Candle, ExecutionResult, ExitRules, Portfolio, Position, TradingMode, utc_now


@dataclass
class Lot:
    position: Position
    cost: float  # USDT spent incl. fees


@dataclass
class ClosedTrade:
    symbol: str
    strategy: str
    entry_time: datetime
    entry_price: float
    exit_time: datetime
    exit_price: float
    quantity: float
    pnl: float
    exit_reason: Optional[str]


class PortfolioBook:
    def __init__(self, starting_capital: float, mode: TradingMode) -> None:
        self.starting_capital = starting_capital
        self.cash = starting_capital
        self.realized_pnl = 0.0
        self.lots: list[Lot] = []
        self.prices: dict[str, float] = {}
        self.bars_since_exit: dict[str, int] = {}
        self.mode = mode
        self.closed: list[ClosedTrade] = []
        self._day: Optional[date] = None
        self.daily_pnl = 0.0
        self.trades_today = 0
        self.now: datetime = utc_now()

    # ------------------------------------------------------------------ bars / marks
    def on_bar(self, symbol: str, bar: Candle, now: datetime) -> None:
        self.now = now
        if self._day != now.date():
            self._day, self.daily_pnl, self.trades_today = now.date(), 0.0, 0
        if symbol in self.bars_since_exit:
            self.bars_since_exit[symbol] += 1
        self.prices[symbol] = bar.close

    def raise_highs(self, symbol: str, high: float) -> None:
        for lot in self.lots:
            p = lot.position
            if p.symbol == symbol:
                lot.position = p.model_copy(update={"highest_price": max(p.highest_price or p.avg_entry_price, high)})

    def mark(self, symbol: str, price: float) -> None:
        self.prices[symbol] = price

    # ------------------------------------------------------------------ fills
    def open_lot(self, fill: ExecutionResult, spent: float, strategy: str, exit_rules: Optional[ExitRules], opened_at: datetime) -> Position:
        px = fill.avg_price or (spent / fill.filled_quantity)
        x = exit_rules
        pos = Position(
            symbol=fill.symbol, quantity=fill.filled_quantity, avg_entry_price=px, opened_at=opened_at, strategy=strategy,
            current_price=px, highest_price=px,
            stop_loss_price=px * (1 - x.stop_loss_pct / 100) if x else None,
            take_profit_price=px * (1 + x.take_profit_pct / 100) if x else None,
            trailing_stop_pct=x.trailing_stop.pct if x and x.trailing_stop.enabled else None,
        )
        self.cash -= spent
        self.lots.append(Lot(pos, spent))
        self.prices.setdefault(fill.symbol, px)
        self.trades_today += 1
        return pos

    def close_lot(self, lot: Lot, fill: ExecutionResult, reason: Optional[str], closed_at: datetime) -> ClosedTrade:
        proceeds = fill.filled_quantity * (fill.avg_price or 0.0) - fill.fee
        pnl = proceeds - lot.cost
        self.cash += proceeds
        self.realized_pnl += pnl
        self.daily_pnl += pnl
        self.trades_today += 1
        self.lots.remove(lot)
        self.bars_since_exit[lot.position.symbol] = 0
        p = lot.position
        t = ClosedTrade(symbol=p.symbol, strategy=p.strategy, entry_time=p.opened_at, entry_price=p.avg_entry_price,
                        exit_time=closed_at, exit_price=fill.avg_price or 0.0, quantity=fill.filled_quantity, pnl=pnl, exit_reason=reason)
        self.closed.append(t)
        return t

    # ------------------------------------------------------------------ persistence (24/7 restarts, PAPER/LIVE)
    def snapshot(self) -> dict:
        return {
            "starting_capital": self.starting_capital, "cash": self.cash, "realized_pnl": self.realized_pnl,
            "lots": [{"position": l.position.model_dump(mode="json"), "cost": l.cost} for l in self.lots],
            "bars_since_exit": dict(self.bars_since_exit), "daily_pnl": self.daily_pnl, "trades_today": self.trades_today,
            "day": self._day.isoformat() if self._day else None, "prices": dict(self.prices),
        }

    def restore(self, d: dict) -> None:
        self.starting_capital = float(d.get("starting_capital", self.starting_capital))
        self.cash = float(d.get("cash", self.cash))
        self.realized_pnl = float(d.get("realized_pnl", 0.0))
        self.lots = [Lot(Position.model_validate(x["position"]), float(x["cost"])) for x in d.get("lots", [])]
        self.bars_since_exit = {k: int(v) for k, v in d.get("bars_since_exit", {}).items()}
        self.daily_pnl = float(d.get("daily_pnl", 0.0))
        self.trades_today = int(d.get("trades_today", 0))
        self._day = date.fromisoformat(d["day"]) if d.get("day") else None
        self.prices = {k: float(v) for k, v in d.get("prices", {}).items()}

    def lots_for(self, symbol: str) -> list[Lot]:
        return [lot for lot in self.lots if lot.position.symbol == symbol]

    # ------------------------------------------------------------------ snapshot
    def portfolio(self) -> Portfolio:
        positions = []
        mv = 0.0
        for lot in self.lots:
            p = lot.position
            px = self.prices.get(p.symbol, p.avg_entry_price)
            value = p.quantity * px
            mv += value
            positions.append(p.model_copy(update={"current_price": px, "unrealized_pnl": round(value - lot.cost, 8)}))
        return Portfolio(
            starting_capital=self.starting_capital, cash=round(max(self.cash, 0.0), 8), positions=positions,
            realized_pnl=round(self.realized_pnl, 8), equity=round(max(self.cash + mv, 0.0), 8),
            bars_since_exit=dict(self.bars_since_exit), trades_today=self.trades_today, daily_pnl=round(self.daily_pnl, 8),
            mode=self.mode, updated_at=self.now,
        )
