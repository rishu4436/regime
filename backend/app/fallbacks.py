"""Fallback implementations of the contract Protocols, used by the app ONLY while a bot's module is still a stub
(raises NotImplementedError). They keep the full loop and the e2e tests runnable today; as soon as Bot 2/3/5
ship, `wiring.py` picks the real classes up automatically. Intentionally simple - not the product logic.
"""
from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Optional, Sequence, Union

from backend.contracts import (
    SIM_WARMUP_BARS, Action, Alert, PerformanceSummary, AgentMode, AutoConfig, BacktestResult, BacktestTrade, Candle, ConditionLogic,
    ControlState, Decision, EquityPoint, ExitReason, ManualStrategy, Portfolio, Position, PresetName, Regime,
    RegimeAnalysis, RiskCheck, RiskVerdict, Signal, StatusSnapshot, StrategySelection, TraderStyle, TradingMode,
    utc_now,
)

from . import indicators as ind
from .exits import ExitHit, check_protective_exit, max_loss_level, trailing_level


# =========================================================================== engine
class _Ind:
    """Indicators for a candle window, computed once."""

    def __init__(self, s: ManualStrategy, candles: Sequence[Candle]) -> None:
        c = [x.close for x in candles]
        e = s.entry
        self.closes = c
        self.rsi_entry = ind.rsi(c, e.rsi_below.period)
        self.rsi_exit = ind.rsi(c, s.exit.rsi_above.period)
        self.sma = ind.sma(c, e.price_above_sma.period)
        self.ema = ind.ema(c, e.price_above_ema.period)
        p = e.macd_bullish_cross.params
        self.macd, self.macd_sig = ind.macd(c, p.fast, p.slow, p.signal)
        px = s.exit.macd_bearish_cross.params
        self.xmacd, self.xmacd_sig = ind.macd(c, px.fast, px.slow, px.signal)
        self.bb_lo, _, _ = ind.bollinger(c, e.bollinger_lower.period, e.bollinger_lower.std_dev)
        vols = [x.volume for x in candles]
        self.vols = vols

    def vol_avg(self, i: int, period: Optional[int]) -> Optional[float]:
        if period is None:
            return sum(self.vols) / len(self.vols) if self.vols else None
        if i + 1 < period:
            return None
        return sum(self.vols[i - period + 1:i + 1]) / period


def _entry_conditions(s: ManualStrategy, x: _Ind, candles: Sequence[Candle], i: int) -> tuple[list[str], list[str]]:
    e, c = s.entry, x.closes[i]
    met: list[str] = []
    failed: list[str] = []

    def add(name: str, value: Optional[bool]) -> None:
        if value is None:
            return  # warm-up: skipped, not false
        (met if value else failed).append(name)

    if e.rsi_below.enabled:
        add("rsi_below", None if x.rsi_entry[i] is None else x.rsi_entry[i] < e.rsi_below.threshold)
    if e.price_above_sma.enabled:
        add("price_above_sma", None if x.sma[i] is None else c > x.sma[i])
    if e.price_above_ema.enabled:
        add("price_above_ema", None if x.ema[i] is None else c > x.ema[i])
    if e.macd_bullish_cross.enabled:
        ok = i > 0 and None not in (x.macd[i], x.macd_sig[i], x.macd[i - 1], x.macd_sig[i - 1])
        add("macd_bullish_cross", (x.macd[i] > x.macd_sig[i] and x.macd[i - 1] <= x.macd_sig[i - 1]) if ok else None)
    if e.bollinger_lower.enabled:
        lo = x.bb_lo[i]
        add("bollinger_lower", None if lo is None else c < lo * (1 + e.bollinger_lower.proximity_pct / 100))
    if e.volume_above_avg.enabled:
        avg = x.vol_avg(i, e.volume_above_avg.period)
        add("volume_above_avg", None if avg is None else candles[i].volume > avg * e.volume_above_avg.multiplier)
    if e.breakout.enabled:
        lb = e.breakout.lookback
        if i >= lb:
            prev = candles[i - lb:i]
            if e.breakout.direction.value == "HIGH":
                add("breakout", c > max(p.high for p in prev))
            else:
                add("breakout", c < min(p.low for p in prev))
    return met, failed


def _filters_ok(s: ManualStrategy, x: _Ind, candles: Sequence[Candle], i: int) -> tuple[bool, str]:
    mh = s.filters.market_hours
    if mh.enabled:
        ts = candles[i].timestamp
        if mh.weekdays_only and ts.weekday() >= 5:
            return False, "market_hours: weekend"
        if not (mh.start_hour_utc <= ts.hour < mh.end_hour_utc):
            return False, f"market_hours: {ts.hour}:00 UTC outside {mh.start_hour_utc}-{mh.end_hour_utc}"
    mv = s.filters.min_volume
    if mv.enabled:
        avg = x.vol_avg(i, s.entry.volume_above_avg.period)
        v = candles[i].volume
        if avg is None or v <= avg * mv.multiplier:
            return False, "min_volume: volume not above average (hard filter)"
        if mv.min_volume is not None and v < mv.min_volume:
            return False, "min_volume: below absolute minimum"
    return True, ""


def _combine(logic: ConditionLogic, met: list[str], failed: list[str]) -> bool:
    if not met and not failed:
        return False
    return not failed if logic == ConditionLogic.AND else bool(met)


def _strategy_exit_on_close(s: ManualStrategy, x: _Ind, i: int) -> Optional[ExitReason]:
    r = s.exit.rsi_above
    if r.enabled and x.rsi_exit[i] is not None and x.rsi_exit[i] > r.threshold:
        return ExitReason.RSI_EXIT
    if s.exit.macd_bearish_cross.enabled and i > 0 and None not in (x.xmacd[i], x.xmacd_sig[i], x.xmacd[i - 1], x.xmacd_sig[i - 1]):
        if x.xmacd[i] < x.xmacd_sig[i] and x.xmacd[i - 1] > x.xmacd_sig[i - 1]:
            return ExitReason.MACD_EXIT
    return None


def _capital(p: Portfolio) -> float:
    return p.starting_capital + p.realized_pnl


# preset table: (regime, style) -> (preset or None, size multiplier)
_P, _B, _A = TraderStyle.PATIENT, TraderStyle.BALANCED, TraderStyle.ACTIVE
PRESET_MAP: dict[Regime, dict[TraderStyle, tuple[Optional[PresetName], float]]] = {
    Regime.STRONG_UPTREND: {_P: (PresetName.SMART_DCA, 1.0), _B: (PresetName.TREND_FOLLOWING, 1.0), _A: (PresetName.TREND_FOLLOWING, 1.0)},
    Regime.MILD_UPTREND: {_P: (PresetName.DCA, 1.0), _B: (PresetName.SMART_DCA, 1.0), _A: (PresetName.TREND_FOLLOWING, 1.0)},
    Regime.SIDEWAYS: {_P: (PresetName.DCA, 1.0), _B: (PresetName.GRID, 1.0), _A: (PresetName.GRID, 1.0)},
    Regime.OVERSOLD_PULLBACK: {_P: (PresetName.BUY_THE_DIP, 1.0), _B: (PresetName.BUY_THE_DIP, 1.0), _A: (PresetName.BUY_THE_DIP, 1.0)},
    Regime.HIGH_VOLATILITY: {_P: (PresetName.DCA, 0.5), _B: (None, 0.0), _A: (PresetName.GRID, 0.5)},
    Regime.DOWNTREND: {_P: (PresetName.DCA, 0.25), _B: (None, 0.0), _A: (None, 0.0)},
}
STYLE_SIZE_PCT = {_P: 5.0, _B: 10.0, _A: 15.0}
DCA_EVERY_BARS = {_P: 24, _B: 12, _A: 6}


class FallbackStrategyEngine:
    """Minimal StrategyEngine. Bot 2's DefaultStrategyEngine replaces it method by method."""

    def detect_regime(self, candles: Sequence[Candle]) -> RegimeAnalysis:
        c = [x.close for x in candles]
        if len(c) < 60:
            return RegimeAnalysis(regime=Regime.SIDEWAYS, confidence=0.2, reason="not enough data (<60 bars)")
        s50, e20, r = ind.sma(c, 50), ind.ema(c, 20), ind.rsi(c, 14)
        rets = [(c[i] / c[i - 1] - 1) for i in range(len(c) - 20, len(c))]
        mean = sum(rets) / len(rets)
        vol = (sum((x - mean) ** 2 for x in rets) / len(rets)) ** 0.5 * 100
        slope = (s50[-1] / s50[-11] - 1) * 100
        dist = (c[-1] / s50[-1] - 1) * 100
        drop10 = (c[-1] / max(c[-10:]) - 1) * 100
        f = {"sma_slope_pct": round(slope, 4), "rsi": round(r[-1], 2), "vol_pct": round(vol, 4),
             "dist_from_sma_pct": round(dist, 4), "drop_10_pct": round(drop10, 4)}
        if vol > 2.0:
            reg, why = Regime.HIGH_VOLATILITY, f"bar volatility {vol:.2f}% > 2%"
        elif r[-1] < 35 and drop10 < -5:
            reg, why = Regime.OVERSOLD_PULLBACK, f"RSI {r[-1]:.0f} < 35 after {drop10:.1f}% drop"
        elif dist < 0 and slope < 0:
            reg, why = Regime.DOWNTREND, "price below falling SMA50"
        elif dist > 6 and slope > 3 and c[-1] > e20[-1]:
            reg, why = Regime.STRONG_UPTREND, f"price {dist:.1f}% above rising SMA50"
        elif dist > 0 and slope > 0.2:
            reg, why = Regime.MILD_UPTREND, "price above gently rising SMA50"
        else:
            reg, why = Regime.SIDEWAYS, "flat SMA50, low volatility"
        return RegimeAnalysis(regime=reg, confidence=0.6, reason=why, features=f, timestamp=candles[-1].timestamp)

    def select_strategy(self, regime: Regime, style: TraderStyle, enabled_presets: Sequence[PresetName]) -> StrategySelection:
        preset, mult = PRESET_MAP[regime][style]
        if preset is None:
            return StrategySelection(regime=regime, style=style, preset=None, size_multiplier=0.0,
                                     reason=f"{regime.value} with {style.value} style -> pause / stay out")
        if preset not in enabled_presets:
            return StrategySelection(regime=regime, style=style, preset=None, size_multiplier=0.0,
                                     reason=f"mapped preset {preset.value} is disabled -> stay out")
        return StrategySelection(regime=regime, style=style, preset=preset, size_multiplier=mult,
                                 reason=f"{regime.value} + {style.value} -> {preset.value}")

    def evaluate_manual(self, strategy: ManualStrategy, candles: Sequence[Candle], portfolio: Portfolio) -> Signal:
        x, i, sym = _Ind(strategy, candles), len(candles) - 1, strategy.symbol
        name = f"MANUAL:{strategy.name}"
        price = candles[-1].close
        base = dict(symbol=sym, source=AgentMode.MANUAL, strategy=name, price=price, timestamp=candles[-1].timestamp)
        open_pos = [p for p in portfolio.positions if p.symbol == sym]
        if open_pos:
            r = _strategy_exit_on_close(strategy, x, i)
            if r is not None:
                return Signal(action=Action.SELL, reason=f"{r.value}: strategy exit on close", exit_reason=r, **base)
        if len(open_pos) >= strategy.risk.max_open_positions:
            return Signal(action=Action.STAY_OUT, reason=f"max open positions ({strategy.risk.max_open_positions}) reached", **base)
        since = portfolio.bars_since_exit.get(sym)
        if since is not None and since <= strategy.risk.cooldown_bars:
            return Signal(action=Action.STAY_OUT, reason=f"cooldown: {since} bars since last exit <= {strategy.risk.cooldown_bars}", **base)
        ok, why = _filters_ok(strategy, x, candles, i)
        if not ok:
            return Signal(action=Action.STAY_OUT, reason=f"filter blocked: {why}", **base)
        met, failed = _entry_conditions(strategy, x, candles, i)
        if _combine(strategy.entry.logic, met, failed):
            size = strategy.risk.position_size_pct
            return Signal(action=Action.BUY, reason=f"entry ({strategy.entry.logic.value}) met: {', '.join(met)}",
                          conditions_met=met, conditions_failed=failed, proposed_size_pct=size,
                          proposed_notional=round(_capital(portfolio) * size / 100, 8), **base)
        why = f"entry ({strategy.entry.logic.value}) not met" + (f": failed {', '.join(failed)}" if failed else ": no condition available yet")
        return Signal(action=Action.STAY_OUT, reason=why, conditions_met=met, conditions_failed=failed, **base)

    def evaluate_auto(self, config: AutoConfig, candles: Sequence[Candle], portfolio: Portfolio) -> Signal:
        ra = self.detect_regime(candles)
        sel = self.select_strategy(ra.regime, config.style, config.enabled_presets)
        c = [x.close for x in candles]
        price, sym = c[-1], config.symbol
        base = dict(symbol=sym, source=AgentMode.AUTO, regime=ra.regime, price=price, timestamp=candles[-1].timestamp)
        if sel.preset is None:
            return Signal(action=Action.STAY_OUT, strategy="NONE", reason=f"{ra.reason}; {sel.reason}", **base)
        p = sel.preset
        holding = [x for x in portfolio.positions if x.symbol == sym]
        e20, s20, s50, r = ind.ema(c, 20), ind.sma(c, 20), ind.sma(c, 50), ind.rsi(c, 14)
        lo, _, _ = ind.bollinger(c, 20, 2.0)
        line, sig = ind.macd(c)
        n = len(c)
        if p == PresetName.TREND_FOLLOWING and holding and e20[-1] and price < e20[-1]:
            return Signal(action=Action.SELL, strategy=p.value, preset=p, reason="trend lost: close < EMA20",
                          conditions_met=["trend_lost"], **base)
        if p == PresetName.DCA:
            go, why = n % DCA_EVERY_BARS[config.style] == 0, f"DCA every {DCA_EVERY_BARS[config.style]} bars"
        elif p == PresetName.SMART_DCA:
            go = n % (DCA_EVERY_BARS[config.style] // 2 or 1) == 0 and bool(s20[-1]) and price < s20[-1] * 1.01
            why = "smart DCA: scheduled buy while close <= SMA20 +1%"
        elif p == PresetName.BUY_THE_DIP:
            go = not holding and r[-1] is not None and r[-1] < 35 and lo[-1] is not None and price < lo[-1] * 1.02
            why = "buy the dip: RSI < 35 and close near/below lower Bollinger band"
        elif p == PresetName.TREND_FOLLOWING:
            go = (not holding and None not in (e20[-1], s50[-1], line[-1], sig[-1])
                  and price > e20[-1] > s50[-1] and line[-1] > sig[-1])
            why = "trend: close > EMA20 > SMA50 and MACD > signal"
        else:  # GRID
            go = len(holding) < 3 and bool(s20[-1]) and price < s20[-1] * 0.99
            why = "grid: close >= 1% below SMA20 grid mid"
        size = STYLE_SIZE_PCT[config.style] * sel.size_multiplier
        cap = min(config.capital, _capital(portfolio))
        if go:
            return Signal(action=Action.BUY, strategy=p.value, preset=p, reason=f"{ra.reason}; {sel.reason}; {why}",
                          conditions_met=[why], proposed_size_pct=size, proposed_notional=round(cap * size / 100, 8), **base)
        return Signal(action=Action.STAY_OUT, strategy=p.value, preset=p, reason=f"{ra.reason}; {sel.reason}; not triggered: {why}",
                      conditions_failed=[why], **base)

    def backtest_manual(self, strategy: ManualStrategy, candles: Sequence[Candle], initial_capital: float) -> BacktestResult:
        x = _Ind(strategy, candles)
        ex = strategy.exit
        capital = initial_capital
        open_: list[dict] = []
        trades: list[BacktestTrade] = []
        curve: list[EquityPoint] = []
        last_exit: Optional[int] = None
        start = min(SIM_WARMUP_BARS, max(len(candles) - 1, 0))
        for i in range(start, len(candles)):
            b = candles[i]
            for p in list(open_):
                hit = check_protective_exit(
                    open_=b.open, high=b.high, low=b.low,
                    stop_loss_price=p["entry"] * (1 - ex.stop_loss_pct / 100),
                    take_profit_price=p["entry"] * (1 + ex.take_profit_pct / 100),
                    trail_level=trailing_level(p["high"], ex.trailing_stop.pct if ex.trailing_stop.enabled else None, p["entry"]),
                    max_loss_price=max_loss_level(p["entry"], p["qty"], strategy.risk.max_loss_per_trade_pct, initial_capital))
                reason, px = (hit.reason, hit.price) if hit else (None, None)
                if reason is None:
                    r = _strategy_exit_on_close(strategy, x, i)
                    if r is not None:
                        reason, px = r, b.close
                if reason is not None:
                    capital += self._close(strategy.symbol, p, px, b.timestamp, reason, trades)
                    open_.remove(p)
                    last_exit = i
                else:
                    p["high"] = max(p["high"], b.high)
            gate = len(open_) < strategy.risk.max_open_positions and (last_exit is None or i - last_exit > strategy.risk.cooldown_bars)
            if gate and _filters_ok(strategy, x, candles, i)[0]:
                met, failed = _entry_conditions(strategy, x, candles, i)
                if _combine(strategy.entry.logic, met, failed):
                    size = capital * strategy.risk.position_size_pct / 100
                    open_.append({"entry": b.close, "size": size, "high": b.close, "t": b.timestamp, "qty": size / b.close})
            unreal = sum(p["size"] * (b.close / p["entry"] - 1) for p in open_)
            curve.append(EquityPoint(timestamp=b.timestamp, equity=round(capital + unreal, 8)))
        if candles:
            for p in open_:
                capital += self._close(strategy.symbol, p, candles[-1].close, candles[-1].timestamp, ExitReason.END, trades)
            if curve:
                curve[-1] = EquityPoint(timestamp=curve[-1].timestamp, equity=round(capital, 8))
        peak, mdd = initial_capital, 0.0
        for pt in curve:
            peak = max(peak, pt.equity)
            mdd = max(mdd, (peak - pt.equity) / peak * 100 if peak else 0)
        wins = sum(1 for t in trades if t.pnl > 0)
        return BacktestResult(
            strategy_name=strategy.name, symbol=strategy.symbol, initial_capital=initial_capital, final_equity=round(capital, 8),
            total_return_pct=round((capital / initial_capital - 1) * 100, 6), max_drawdown_pct=round(mdd, 6),
            win_rate_pct=round(wins / len(trades) * 100, 6) if trades else 0.0, num_trades=len(trades), trades=trades, equity_curve=curve)

    @staticmethod
    def _close(sym: str, p: dict, px: float, t: datetime, reason: ExitReason, trades: list[BacktestTrade]) -> float:
        pnl_pct = (px / p["entry"] - 1) * 100
        pnl = p["size"] * pnl_pct / 100
        trades.append(BacktestTrade(symbol=sym, entry_time=p["t"], entry_price=p["entry"], exit_time=t, exit_price=px,
                                    quantity=p["qty"], pnl=round(pnl, 8), pnl_pct=round(pnl_pct, 6), exit_reason=reason))
        return pnl


# =========================================================================== risk
MAX_EXPOSURE_PCT = 80.0  # of equity
MAX_TRADE_PCT = 25.0  # of equity per entry
DAILY_LOSS_LIMIT_PCT = 5.0  # of starting capital
MIN_NOTIONAL = 1.0


class FallbackRiskManager:
    """Minimal RiskManager. Bot 3's DefaultRiskManager replaces it as a whole (it is stateful)."""

    def __init__(self, control: Optional[ControlState] = None, live_allowed: Optional[bool] = None) -> None:
        self._control = control or ControlState()
        self._max_loss_pct: dict[str, Optional[float]] = {}
        self._live_allowed = live_allowed if live_allowed is not None else os.getenv("LIVE_TRADING_ENABLED", "").lower() == "true"

    def _set(self, **kw) -> ControlState:
        self._control = self._control.model_copy(update={**kw, "updated_at": utc_now()})
        return self._control

    def check(self, signal: Signal, portfolio: Portfolio, control: ControlState) -> RiskVerdict:
        checks: list[RiskCheck] = []

        def chk(name: str, ok: bool, detail: str = "") -> None:
            checks.append(RiskCheck(name=name, passed=ok, detail=detail))

        live_ok = control.trading_mode != TradingMode.LIVE or self._live_allowed
        chk("live_mode_allowed", live_ok, "LIVE requires LIVE_TRADING_ENABLED=true" if not live_ok else "")
        if signal.action == Action.STAY_OUT:
            chk("actionable", False, "signal is STAY_OUT")
        elif signal.action == Action.SELL:
            chk("emergency_stop", True, "exits are always allowed")
            held = any(p.symbol == signal.symbol for p in portfolio.positions)
            chk("has_position", held, "" if held else f"no open position in {signal.symbol}")
        else:
            chk("emergency_stop", not control.emergency_stopped, control.reason or "" if control.emergency_stopped else "")
            chk("paused", not control.paused, control.reason or "" if control.paused else "")
            eq = max(portfolio.equity, 1e-9)
            exposure = sum(p.quantity * (p.current_price or p.avg_entry_price) for p in portfolio.positions)
            notional = min(signal.proposed_notional, eq * MAX_TRADE_PCT / 100, portfolio.cash)
            room = eq * MAX_EXPOSURE_PCT / 100 - exposure
            notional = min(notional, max(room, 0.0))
            chk("max_trade_size", signal.proposed_notional <= eq * MAX_TRADE_PCT / 100 + 1e-9,
                f"proposed {signal.proposed_notional:.2f} vs cap {eq * MAX_TRADE_PCT / 100:.2f} (size reduced)")
            chk("max_exposure", room >= MIN_NOTIONAL, f"exposure {exposure:.2f} / cap {eq * MAX_EXPOSURE_PCT / 100:.2f}")
            chk("cash", portfolio.cash >= MIN_NOTIONAL, f"cash {portfolio.cash:.2f}")
            limit = portfolio.starting_capital * DAILY_LOSS_LIMIT_PCT / 100
            chk("daily_loss_limit", portfolio.daily_pnl > -limit, f"daily pnl {portfolio.daily_pnl:.2f} / limit -{limit:.2f}")
            chk("min_notional", notional >= MIN_NOTIONAL, f"notional {notional:.2f}")
        hard = [c for c in checks if not c.passed and c.name != "max_trade_size"]
        approved = not hard
        size_pct = 0.0
        adj = 0.0
        if approved and signal.action == Action.BUY:
            adj = round(notional, 8)
            size_pct = min(100.0, adj / max(portfolio.equity, 1e-9) * 100)
        elif approved:
            adj, size_pct = signal.proposed_notional, signal.proposed_size_pct
        return RiskVerdict(
            signal_id=signal.id, approved=approved, checks=checks,
            reasons=[f"{c.name}: {c.detail}".rstrip(": ") for c in hard] or ["all risk checks passed"],
            adjusted_size_pct=size_pct, adjusted_notional=adj,
            requires_confirmation=approved and signal.action == Action.BUY and control.confirmation_mode,
        )

    def apply_manual_strategy(self, strategy: ManualStrategy) -> None:
        """Same optional hook as Bot 3's DefaultRiskManager: per-symbol rules from the active manual strategy."""
        self._max_loss_pct[strategy.symbol] = strategy.risk.max_loss_per_trade_pct

    def check_exits(self, portfolio: Portfolio, prices: dict[str, float], bars: Optional[dict[str, Candle]] = None) -> list[Signal]:
        """Never mutates `portfolio`. Emergency stop -> EMERGENCY_STOP exit for every position at the close / last price."""
        out: list[Signal] = []
        for pos in portfolio.positions:
            bar = (bars or {}).get(pos.symbol)
            px = bar.close if bar else (prices.get(pos.symbol) or pos.current_price)
            if not px:
                continue
            o, h, l = (bar.open, bar.high, bar.low) if bar else (px, px, px)
            if self._control.emergency_stopped:
                hit = ExitHit(ExitReason.EMERGENCY_STOP, px)
            else:
                hit = check_protective_exit(
                    open_=o, high=h, low=l, stop_loss_price=pos.stop_loss_price, take_profit_price=pos.take_profit_price,
                    trail_level=trailing_level(pos.highest_price, pos.trailing_stop_pct, pos.avg_entry_price),
                    max_loss_price=max_loss_level(pos.avg_entry_price, pos.quantity, self._max_loss_pct.get(pos.symbol), portfolio.starting_capital))
            if hit:
                out.append(Signal(symbol=pos.symbol, action=Action.SELL, source=AgentMode.MANUAL if pos.strategy.startswith("MANUAL:") else AgentMode.AUTO,
                                  strategy=pos.strategy, reason=f"{hit.reason.value} hit at {hit.price:.6g}",
                                  exit_reason=hit.reason, position_id=pos.id, price=hit.price,
                                  proposed_notional=round(pos.quantity * hit.price, 8),
                                  timestamp=bar.timestamp if bar else utc_now()))
        return out

    def get_control_state(self) -> ControlState:
        return self._control

    def pause(self, reason: str = "") -> ControlState:
        return self._set(paused=True, reason=reason or "paused by user")

    def resume(self) -> ControlState:
        """Clears `paused` only; an emergency stop stays until reset_emergency_stop() (contract v0.2)."""
        return self._set(paused=False, reason=self._control.reason if self._control.emergency_stopped else None)

    def emergency_stop(self, reason: str = "") -> ControlState:
        return self._set(emergency_stopped=True, reason=reason or "emergency stop")

    def reset_emergency_stop(self) -> ControlState:
        return self._set(emergency_stopped=False, reason=None if not self._control.paused else self._control.reason)

    def set_confirmation_mode(self, enabled: bool) -> ControlState:
        return self._set(confirmation_mode=enabled)


# =========================================================================== explainer
class FallbackExplainer:
    """Deterministic templates. Bot 5's DefaultExplainer replaces it method by method."""

    def explain_decision(self, decision: Decision) -> str:
        parts = [f"{decision.action.value} {decision.symbol} via {decision.strategy}"]
        if decision.regime:
            parts.append(f"in a {decision.regime.value.replace('_', ' ').lower()} market")
        txt = " ".join(parts) + f": {decision.reason}."
        v = decision.risk_verdict
        if v is not None and not v.approved:
            txt += " Risk layer blocked it: " + "; ".join(v.reasons) + "."
        if decision.execution is not None:
            txt += f" Order status: {decision.execution.status.value}."
        return txt

    def summarize_state(self, status: StatusSnapshot, *, performance: Optional[PerformanceSummary] = None,
                        alerts: Optional[list[Alert]] = None) -> str:
        txt = self._summary(status)
        if performance is not None:
            txt += (f" Performance: return {performance.total_return_pct:+.2f}%, max drawdown {performance.max_drawdown_pct:.2f}%, "
                    f"{performance.num_trades} closed trade(s), win rate {performance.win_rate_pct:.0f}%.")
        if alerts:
            txt += f" Latest alert: [{alerts[0].level.value}] {alerts[0].message}."
        return txt

    def _summary(self, status: StatusSnapshot) -> str:
        p = status.portfolio
        c = status.control
        state = "EMERGENCY STOPPED" if c.emergency_stopped else "PAUSED" if c.paused else "running"
        return (f"Agent is {state} in {c.trading_mode.value} mode. Mode: {status.agent_mode.value if status.agent_mode else 'none'}, "
                f"symbol {status.active_symbol or '-'}, regime {status.current_regime.value if status.current_regime else '-'}, "
                f"strategy {status.active_strategy or '-'}. Equity {p.equity:.2f} USDT, cash {p.cash:.2f}, "
                f"{len(p.positions)} open position(s), realized PnL {p.realized_pnl:.2f}.")

    def parse_strategy_from_text(self, text: str, default_symbol: Optional[str] = None) -> Union[ManualStrategy, AutoConfig]:
        t = text.lower()
        sym = default_symbol or "NVDAon"
        for s in ("NVDAon", "TSLAx", "AAPLB", "SPYon", "METAx"):
            if s.lower() in t:
                sym = s
        if "auto" in t or "style" in t or any(st.value.lower() in t for st in TraderStyle):
            style = next((st for st in TraderStyle if st.value.lower() in t), TraderStyle.BALANCED)
            cap = re.search(r"(\d+(?:\.\d+)?)\s*(?:usdt|usd|\$)", t)
            return AutoConfig(symbol=sym, style=style, capital=float(cap.group(1)) if cap else 1000.0)
        s = ManualStrategy(symbol=sym, name="Parsed strategy")
        d = s.model_dump()
        if m := re.search(r"rsi\s*(?:<|below|under)\s*(\d+)", t):
            d["entry"]["rsi_below"].update(enabled=True, threshold=float(m.group(1)))
        if m := re.search(r"(?:take profit|tp)\D*(\d+(?:\.\d+)?)", t):
            d["exit"]["take_profit_pct"] = float(m.group(1))
        if m := re.search(r"(?:stop loss|sl)\D*(\d+(?:\.\d+)?)", t):
            d["exit"]["stop_loss_pct"] = float(m.group(1))
        return ManualStrategy.model_validate(d)

    def answer(self, question: str, status: StatusSnapshot, *, performance: Optional[PerformanceSummary] = None,
               alerts: Optional[list[Alert]] = None) -> str:
        return self.summarize_state(status, performance=performance, alerts=alerts)
