# `backend/engine` — Bot 2 — Strategy & Regime Engine

**Owner:** Bot 2 — Strategy & Regime Engine. This bot edits only this directory.

## Scope (verbatim from the spec PDF)
**Goal:** Own all strategy evaluation and regime logic.

- Implement Regime Detector with the regimes listed above
- Implement all pre-set agents (DCA, Smart DCA, Buy the Dip, Trend, Grid)
- Trader style mapping (Patient / Balanced / Active)
- Manual strategy rule evaluator (must match simulator logic)
- Strict single-strategy-at-a-time execution in Auto Mode
- Clear structured decision output (regime, strategy, reason, action)

## Interface it must implement
Implement `backend.contracts.StrategyEngine` in `engine.py` (`DefaultStrategyEngine`):

```python
detect_regime(candles) -> RegimeAnalysis
select_strategy(regime, style, enabled_presets) -> StrategySelection   # exactly one preset or None (stay out)
evaluate_manual(strategy: ManualStrategy, candles, portfolio) -> Signal
evaluate_auto(config: AutoConfig, candles, portfolio) -> Signal      # regime -> map(regime, style) -> evaluate that preset only
backtest_manual(strategy, candles, initial_capital) -> BacktestResult
```
Pure functions: no I/O, no execution, no risk decisions (Bot 3 does those). Indicator formulas, condition
combination (AND/OR, skipped warm-up conditions), exit priority and cooldown semantics are specified in the
docstrings of `backend/contracts/models.py` and in `reference/README.md`. `backtest_manual` with a
simulator-equivalent config must reproduce the simulator's trade list on the same candles.

## Definition of done
- Implements the interface above exactly (signatures from `backend/contracts/interfaces.py`, data shapes from `backend/contracts/models.py`). `isinstance(obj, <Protocol>)` is True.
- Unit tests live in this directory's `tests/` and pass with `pytest` from the repo root; the whole suite stays green.
- Works fully in **SIM** mode with `backend.mocks` (no network, no API keys).
- No edits outside the owned directory. Contract changes go through the coordinator (see CONTRIBUTING.md).

---

# Implementation (Bot 2)

Status: `DefaultStrategyEngine` implements every `StrategyEngine` method. No I/O, deterministic, SIM-only friendly.

## Module map
| File | What |
|---|---|
| `engine.py` | `DefaultStrategyEngine(fee_pct=0.0, slippage_bps=0.0, warmup_bars=60)`, the Protocol implementation |
| `indicators.py` | Causal indicators: `sma`, `ema`, `rsi` (Wilder), `macd`, `bollinger`, `volume_average`, `atr`, `rolling_max/min`, `crossed_above/below`; `rsi_simulator` (the old buggy formula, only for parity tests) |
| `regime.py` | `detect_regime(candles)`: 6 regimes, thresholds as module constants |
| `presets.py` | `PRESET_MAP` (regime x style → ordered preset preferences + size multiplier), `STYLE_PARAMS`, `BASE_SIZE_PCT`, `evaluate_preset()` for the 5 presets |
| `manual.py` | Manual rule evaluation shared by live + backtest: indicators, AND/OR entry, hard filters, RSI/MACD exits |
| `exits.py` | Intrabar price exits (TP/SL/trailing/max-loss), the rules agreed with Bot 3 |
| `backtest.py` | `run_backtest()`: the deterministic bar loop; `BacktestRun` = `BacktestResult` + per-trade notes |

## API
```python
from backend.engine import DefaultStrategyEngine
eng = DefaultStrategyEngine()                      # fee_pct / slippage_bps defaults for backtests (0 = simulator parity)
eng.detect_regime(candles) -> RegimeAnalysis       # regime, confidence, reason, features (rsi, sma_slope_pct, volatility_pct, atr_pct, ...)
eng.select_strategy(regime, style, enabled) -> StrategySelection   # exactly one preset, or preset=None (stay out)
eng.evaluate_manual(strategy, candles, portfolio) -> Signal        # BUY / SELL / STAY_OUT for the LAST candle
eng.evaluate_auto(config, candles, portfolio) -> Signal            # regime -> select -> evaluate ONLY that preset
eng.backtest_manual(strategy, candles, initial_capital, fee_pct=None, slippage_bps=None) -> BacktestResult
eng.backtest_manual_detailed(...) -> BacktestRun   # .result + .notes[i].entry_reason / .exit_detail (aligned with trades)
```
Extra keyword args (`fee_pct`, `slippage_bps`) are optional, so the Protocol signature is unchanged.
Candles are sorted by timestamp defensively. `Signal.conditions_met/failed` use the `EntryConditions` field names
(`rsi_below`, `price_above_sma`, ...), filter names (`market_hours`, `min_volume`), gate names (`cooldown`,
`max_open_positions`) or, for SELLs, the `ExitReason` value.

### Indicators
- SMA: mean of the last n closes. EMA: k = 2/(n+1), seeded with the SMA of the first n values.
- RSI: standard Wilder. Seed = mean of the first n gains/losses, then `avg = (prev*(n-1) + x)/n`.
  RSI = 100 if avg loss = 0 (and gain > 0); RSI = 50 for a perfectly flat series (both 0).
- MACD: line = EMA12 − EMA26; signal = a real 9-period EMA of the line (alpha 2/10 = 0.2), seeded with the SMA of the
  first 9 line values; histogram = line − signal.
- Bollinger: SMA20 ± 2 · population standard deviation.
- Volume average: rolling mean of the last 20 volumes, including the current bar. If `period` is `None` (the contract
  default), the engine uses 20 (`manual.DEFAULT_VOLUME_PERIOD`).
- Every series is causal (value i depends only on bars 0..i). Missing values are `None` and are always checked with
  `is None`, so a MACD of exactly 0.0 is a valid value.

### Manual evaluator (live, `evaluate_manual`)
1. Exits first, for each open position in `strategy.symbol`: the price exits below on the last bar's OHLC, using
   `Position.highest_price` as the carried trailing high-water mark, then the RSI exit and the MACD bearish cross on the
   close. If one triggers, the signal is **SELL** for 100% of the position.
2. Entry gate: fewer than `max_open_positions` held, `bars_since_exit[symbol] > cooldown_bars` (a missing key means no
   cooldown), and the hard filters below.
3. Entry conditions: each enabled condition whose indicator is ready gives one boolean. Conditions still warming up are
   skipped. AND means all true, OR means any true, and no booleans means no entry. A BUY proposes
   `position_size_pct` of `portfolio.equity`.
- **Min-volume is a HARD filter.** It is always ANDed and is never one of the entry conditions: volume >
  multiplier × the same rolling average, plus `volume >= min_volume` when that is set. While the average is still warming
  up, the filter fails, so there is no entry.
- **Market hours** use the candle timestamp in UTC: `[start_hour_utc, end_hour_utc)`, weekdays only if
  `weekdays_only` is set. A window where start > end wraps past midnight. Naive timestamps are treated as UTC.
- **Breakout** (HIGH: close > max high of the previous `lookback` bars; LOW: close < min low) and
  `max_loss_per_trade_pct` are supported. Both are off by default.

### Exit semantics (agreed with Bot 3 `check_exits`; Bot 6 parity-tests these)
For each open position on each bar after its entry bar (an entry fills at its bar's close, so that bar is never checked):
1. **Stop level** = the highest of: the stop loss `entry*(1-sl%)`, the trailing level carried from the previous bar,
   and the max-loss level `entry - capital_at_entry*max_loss%/qty`. The reason comes from whichever rule owns that
   level. On an exact tie the order is TRAILING_STOP > STOP_LOSS > MAX_LOSS.
2. **Gap down**: `open <= stop` → exit at the **open**.
3. **Gap up**: `open >= TP level` (`entry*(1+tp%)`) → TAKE_PROFIT at the **open**.
4. `low <= stop` → exit at the stop level. **If TP and SL are both hit in the same bar, the stop wins** (conservative).
5. `high >= TP` → TAKE_PROFIT at the TP level.
6. If nothing triggers, ratchet the trail: `trail = max(trail, bar_high*(1-trail%))`. The new level is used from the
   next bar on. The initial trail is `entry*(1-trail%)`.
7. Then RSI exit (`RSI > threshold`) and MACD bearish cross on the close, filled at the close.

### Backtest (`backtest_manual`)
- Bar loop from `warmup_bars` (60, the same as the simulator): exits, then entry at the close (never on the last bar),
  then mark-to-market equity `cash + Σ qty·close`. **The equity curve has one point per input candle.**
- Size: `position_size_pct` % of current mark-to-market equity, capped by the cash available.
- Costs: `fee_pct` % of the notional on each leg. Slippage `slippage_bps` raises BUY fills and lowers SELL fills (gap and
  level fills too). Both are 0 by default, for parity with the simulator.
- At the end, open positions are closed at the last close with reason `END`, and the last equity point is the final
  equity.
- Stats: `total_return_pct`; `max_drawdown_pct` over the mark-to-market curve (the peak starts at the initial capital);
  `win_rate_pct` = trades with pnl > 0 / all closed trades, including END; `num_trades` = closed trades; `pnl_pct` is
  relative to the cash spent, including the entry fee.
- Deterministic: the same inputs give an identical `BacktestResult`. There are no random ids in the result.

### Regime detector
Features are taken on the last bar: SMA50, EMA20, RSI14, ATR14%, the per-bar log-return stdev over 20 bars, the SMA50
slope over 10 bars, the 10-bar return, and the distance from SMA/EMA. Rules are checked in order and the first match
wins:

| Order | Regime | Rule |
|---|---|---|
| 1 | HIGH_VOLATILITY | return stdev ≥ 2% |
| 2 | OVERSOLD_PULLBACK | RSI < 30 and 10-bar return ≤ −8% |
| 3 | DOWNTREND | close < SMA50 and slope ≤ −1% |
| 4 | STRONG_UPTREND | close > EMA20 > SMA50, slope ≥ 2.9%, close ≥ 7% above SMA50 |
| 5 | MILD_UPTREND | close > SMA50 and slope ≥ 0.75% |
| 6 | SIDEWAYS | everything else |

With fewer than 61 candles the result is SIDEWAYS with confidence 0.1. The thresholds were tuned on 1h mock candles
and classify 99% of 480 seeded mock series correctly.

### Auto mode: style mapping (first ENABLED preset wins; none → stay out)
| Regime | PATIENT | BALANCED | ACTIVE |
|---|---|---|---|
| STRONG_UPTREND | Smart DCA › DCA › Trend | Trend › Smart DCA › DCA | Trend › Smart DCA |
| MILD_UPTREND | Smart DCA › DCA | Smart DCA › Trend › DCA | Trend › Smart DCA |
| SIDEWAYS | DCA › Smart DCA › Grid | Grid › Smart DCA › DCA | Grid › Buy the Dip |
| OVERSOLD_PULLBACK | Smart DCA › Buy the Dip › DCA | Buy the Dip › Smart DCA | Buy the Dip |
| HIGH_VOLATILITY | DCA › Smart DCA (×0.5) | **pause** | Grid (×0.5) |
| DOWNTREND | DCA (×0.25, minimal) | **pause** | **pause** |

BUY size = `BASE_SIZE_PCT[style]` (5 / 10 / 15 %) × multiplier, applied to `AutoConfig.capital`. SELL = the whole
position.

Presets (`STYLE_PARAMS` per style):
- **DCA**: buy on a clock-aligned schedule (every 24/12/6 bars).
- **Smart DCA**: the same schedule, but skips the buy when the price is stretched (RSI ≥ 45/50/55 *and* close more than
  0/1/2 % above SMA20).
- **Buy the Dip**: buy when RSI < 30/32/35 and close is ≥ 8/6/4 % below the 20-bar high. Sell when RSI > 55/55/50.
- **Trend**: buy when close > EMA20 > SMA50 and MACD > signal. Sell when close < EMA20 or MACD < signal.
- **Grid**: the range is the low/high of the previous 50/40/30 bars, split into 4/5/6 cells. Buy in the bottom cell
  (never below the range). Sell in the top cell when holding.

## Differences from the reference simulator
| # | Simulator | Engine | Why |
|---|---|---|---|
| 1 | RSI seeds with **sums** and smooths them as averages; `loss \|\| 1` | Standard Wilder; 100 when there are no losses, 50 when flat | Correct formula; `rsi_simulator` is kept and a test shows where the two differ |
| 2 | MACD signal alpha 0.15, seeded with the first line value | True 9-EMA (alpha 0.2), seeded with the SMA of 9 line values | Correct formula |
| 3 | Truthiness checks (`macd.line[i] && ...`, `!mid[i]`) | `is None` checks | A value of 0 is valid |
| 4 | Volume average over the **whole** series (look-ahead) | Rolling 20, including the current bar, even when `period=None` | No look-ahead (kickoff decision) |
| 5 | `minVol` is pushed into the entry conditions, so with OR it can trigger an entry | Hard filter, always ANDed (+ optional absolute `min_volume`) | Contract decision |
| 6 | Market hours faked as `i % 24 < 16` | Real UTC 13:00–20:00 on weekdays, using candle timestamps | Contract decision |
| 7 | TP/SL/trailing checked on the close only | Intrabar high/low; SL wins a same-bar tie; gaps fill at the open; trail checked against the previous bar's level, then ratcheted with this bar's high | Realistic; agreed with Bot 3 |
| 8 | Exits fill at the close | Fill at the stop/target level, or at the open on a gap | Same |
| 9 | Equity is realised-only and `continue` skips `equity.push` | Mark-to-market every bar, one point per candle | Drawdown was understated |
| 10 | Sizing uses realised capital | Mark-to-market equity, capped by cash | Consistent with the M2M curve |
| 11 | No fees or slippage | `fee_pct` / `slippage_bps` (default 0) | Real SWAP/RFQ fills cost money |
| 12 | `Math.random` candles | Seeded `backend.mocks` candles, deterministic backtest | Reproducible tests |
| 13 | Can enter on the last bar and close it immediately as `End` | No entry on the last bar | Avoids meaningless zero-length trades |
| 14 | No breakout or max-loss | `entry.breakout`, `risk.max_loss_per_trade_pct` (off by default) | PDF features |

Because of 1, 2, 4 and 7, the engine **will not reproduce the simulator's trade list bar-for-bar**, even on identical
candles. That is intentional: the kickoff required the fixes. The README line above ("must reproduce the simulator's
trade list") is superseded.

## Tests
`pytest backend/engine` runs 103 tests:
- indicators against hand-computed values and independent pandas references
- regime detection on all mock shapes and 25 seeds per regime
- the full preset map and evaluate_auto
- evaluator gates and filters
- every exit rule in isolation and inside the backtest
- backtest determinism and invariants, mark-to-market drawdown, fees and slippage, edge cases
- live/backtest consistency
