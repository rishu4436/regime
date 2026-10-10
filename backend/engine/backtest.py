"""Deterministic manual-strategy backtester (pure function of strategy + candles + capital)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Sequence

from backend.contracts import (
    SIM_WARMUP_BARS,
    BacktestResult,
    BacktestTrade,
    Candle,
    EquityPoint,
    ExitReason,
    ManualStrategy,
)

from . import exits
from .manual import ManualIndicators, check_filters, evaluate_entry, indicator_exit


@dataclass
class _OpenPosition:
    entry_index: int
    entry_time: datetime
    entry_price: float  # fill price incl. slippage
    quantity: float
    cost: float  # cash spent incl. entry fee
    trail: Optional[float]
    max_loss_level: Optional[float]


@dataclass
class TradeNote:
    """Per-trade explanation kept outside the contract model (BacktestTrade has no free-text field)."""

    entry_reason: str
    exit_detail: str = ""


@dataclass
class BacktestRun:
    result: BacktestResult
    notes: list[TradeNote] = field(default_factory=list)  # aligned with result.trades


def run_backtest(
    strategy: ManualStrategy,
    candles: Sequence[Candle],
    initial_capital: float,
    fee_pct: float = 0.0,
    slippage_bps: float = 0.0,
    warmup_bars: int = SIM_WARMUP_BARS,
) -> BacktestRun:
    """Bar loop (oldest -> newest). For every bar i >= warmup_bars:
      1. exits for each open position opened BEFORE bar i (price exits per ``exits`` module, then RSI/MACD on close);
      2. entry at the CLOSE if: not the last bar, open positions < max, bars since last exit > cooldown,
         hard filters pass (market hours, min volume) and the AND/OR entry conditions pass;
      3. mark-to-market equity = cash + sum(qty * close).
    Remaining positions are closed at the last close with reason END. Fees are charged on both legs as
    ``fee_pct`` % of the traded notional; slippage moves BUY fills up and SELL fills down by ``slippage_bps``."""
    if initial_capital <= 0:
        raise ValueError("initial_capital must be > 0")
    if fee_pct < 0 or slippage_bps < 0:
        raise ValueError("fee_pct and slippage_bps must be >= 0")
    cs = sorted(candles, key=lambda c: c.timestamp)
    n = len(cs)
    fee = fee_pct / 100
    slip = slippage_bps / 10_000
    mi = ManualIndicators.compute(strategy, cs)
    risk, ex = strategy.risk, strategy.exit
    trail_pct = ex.trailing_stop.pct if ex.trailing_stop.enabled else None

    cash = initial_capital
    open_pos: list[_OpenPosition] = []
    trades: list[BacktestTrade] = []
    notes: list[TradeNote] = []
    entry_reasons: dict[int, str] = {}
    curve: list[EquityPoint] = []
    last_exit: Optional[int] = None

    def close(p: _OpenPosition, i: int, price: float, reason: ExitReason, detail: str) -> None:
        nonlocal cash
        fill = price * (1 - slip)
        proceeds = p.quantity * fill
        sell_fee = proceeds * fee
        cash += proceeds - sell_fee
        pnl = proceeds - sell_fee - p.cost
        trades.append(
            BacktestTrade(
                symbol=strategy.symbol,
                entry_time=p.entry_time,
                entry_price=p.entry_price,
                exit_time=cs[i].timestamp,
                exit_price=fill,
                quantity=p.quantity,
                pnl=pnl,
                pnl_pct=pnl / p.cost * 100,
                exit_reason=reason,
            )
        )
        notes.append(TradeNote(entry_reason=entry_reasons.pop(id(p), ""), exit_detail=detail))

    for i in range(n):
        c = cs[i]
        if i >= warmup_bars:
            # 1. exits
            still: list[_OpenPosition] = []
            for p in open_pos:
                if p.entry_index >= i:
                    still.append(p)
                    continue
                pe = exits.check_price_exit(
                    c.open,
                    c.high,
                    c.low,
                    p.entry_price,
                    ex.take_profit_pct,
                    ex.stop_loss_pct,
                    p.trail,
                    p.max_loss_level,
                )
                if pe is not None:
                    close(
                        p,
                        i,
                        pe.price,
                        pe.reason,
                        f"{pe.reason.value} {'gap fill at open' if pe.gap else 'at level'} {pe.price:.6g}",
                    )
                    last_exit = i
                    continue
                if p.trail is not None and trail_pct is not None:
                    p.trail = exits.ratchet_trail(p.trail, c.high, trail_pct)
                ie = indicator_exit(strategy, mi, i)
                if ie is not None:
                    close(p, i, c.close, ie[0], ie[1])
                    last_exit = i
                    continue
                still.append(p)
            open_pos = still

            # 2. entry
            cooled = last_exit is None or i - last_exit > risk.cooldown_bars
            if i < n - 1 and len(open_pos) < risk.max_open_positions and cooled:
                f_ok, _ = check_filters(strategy, mi, cs, i)
                if f_ok:
                    ev = evaluate_entry(strategy, mi, cs, i)
                    if ev.ok:
                        equity_now = cash + sum(p.quantity * c.close for p in open_pos)
                        notional = min(equity_now * risk.position_size_pct / 100, cash / (1 + fee))
                        if notional > 0:
                            fill = c.close * (1 + slip)
                            qty = notional / fill
                            cost = notional * (1 + fee)
                            cash -= cost
                            p = _OpenPosition(
                                entry_index=i,
                                entry_time=c.timestamp,
                                entry_price=fill,
                                quantity=qty,
                                cost=cost,
                                trail=exits.initial_trail(fill, trail_pct) if trail_pct is not None else None,
                                max_loss_level=exits.max_loss_level(fill, qty, equity_now, risk.max_loss_per_trade_pct),
                            )
                            open_pos.append(p)
                            entry_reasons[id(p)] = f"{strategy.entry.logic.value}: " + "; ".join(ev.detail)
        # 3. mark to market
        curve.append(EquityPoint(timestamp=c.timestamp, equity=cash + sum(p.quantity * c.close for p in open_pos)))

    if open_pos:
        for p in open_pos:
            close(p, n - 1, cs[-1].close, ExitReason.END, "backtest finished")
        curve[-1] = EquityPoint(timestamp=cs[-1].timestamp, equity=cash)

    final = cash
    peak, mdd = initial_capital, 0.0
    for pt in curve:
        peak = max(peak, pt.equity)
        mdd = max(mdd, (peak - pt.equity) / peak)
    wins = sum(1 for t in trades if t.pnl > 0)
    result = BacktestResult(
        strategy_name=strategy.name,
        symbol=strategy.symbol,
        initial_capital=initial_capital,
        final_equity=final,
        total_return_pct=(final - initial_capital) / initial_capital * 100,
        max_drawdown_pct=mdd * 100,
        win_rate_pct=wins / len(trades) * 100 if trades else 0.0,
        num_trades=len(trades),
        trades=trades,
        equity_curve=curve,
    )
    return BacktestRun(result=result, notes=notes)
