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
| `engine.py` | `DefaultStrategyEngine(fee_pct=0.0, slippage_bps=0.0, warmup_bars=60, manual_price_exits=False, live_safe=False, live_config=None, clock=utc_now)`, the Protocol implementation |
| `data.py` | `sanitize_candles()` + `DataReport`, timeframe inference, gap / dead-bar helpers, token-type suffix rule, staleness |
| `live.py` | Live / mainnet safety layer: `LiveSafetyConfig`, `entry_blockers()`, `cap_size_pct()`, liquidity measures |
| `indicators.py` | Causal indicators: `sma`, `ema`, `rsi` (Wilder), `macd`, `bollinger`, `volume_average`, `atr`, `rolling_max/min`, `crossed_above/below`; `rsi_simulator` (the old buggy formula, only for parity tests) |
| `regime.py` | `detect_regime(candles, timeframe=None)`: 6 regimes, thresholds as module constants scaled by the bar length |
| `presets.py` | `PRESET_MAP` (regime x style → ordered preset preferences + size multiplier), `STYLE_PARAMS`, `BASE_SIZE_PCT`, `evaluate_preset()` for the 5 presets |
| `manual.py` | Manual rule evaluation shared by live + backtest: indicators, AND/OR entry, hard filters, RSI/MACD exits |
| `exits.py` | Intrabar price exits (TP/SL/trailing/max-loss): the contracts v0.2 `ExitRules` rules, identical to Bot 3's `backend/risk/exits.py` |
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
1. Exits first, for the open positions in `strategy.symbol`, oldest first:
   - **Price exits** (SL, TP, trailing, max-loss) are **off by default**. The live loop runs
     `RiskManager.check_exits` (Bot 3) *before* the engine, and then raises `Position.highest_price` with the current
     bar's high. Checking price exits again here would fire them twice and use a high-water mark that already includes
     this bar. `DefaultStrategyEngine(manual_price_exits=True)` turns them on, using the rules below with
     `Position.highest_price` as the prior-bar high-water mark, and `Position.stop_loss_price` / `take_profit_price` /
     `trailing_stop_pct` overriding the strategy's percentages, the same way Bot 3 does.
   - Then the RSI exit and the MACD bearish cross, on the close.
   - **SELL signal (contracts v0.2):** one Signal per call, for one position. It sets `exit_reason`, `position_id`
     (`Position.id`), `price` = the intended fill (the level, the open on a gap, or the close for RSI/MACD exits), and
     `proposed_notional = qty × price`. If several positions qualify, the oldest is chosen and the reason text says how
     many others also qualify.
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

### Exit semantics (contracts v0.2 `ExitRules`, identical to Bot 3's `check_exits`; parity-tested by Bot 6)
For each open position on each bar after its entry bar (an entry fills at its bar's close, so that bar is never checked):
1. Hit tests, each against its own level:
   - stop loss `low <= entry*(1-sl%)`
   - trailing `low < trail` (**strict**), where `trail = hwm_prev*(1-trail%)` and `hwm_prev` is the highest high of the
     **prior** bars since entry, starting at the entry price. A bar's own high never tightens its own stop.
   - max loss `low <= entry - starting_capital*max_loss%/qty`
   - take profit `high >= entry*(1+tp%)`
2. If **any stop** is hit, it beats the take profit, even when TP is also touched and even when the bar opened above
   TP. Among the stops that were hit, the **highest level wins**, because it is touched first on the way down. Exact
   ties go TRAILING_STOP > STOP_LOSS > MAX_LOSS. The fill is the level, or the **open** if `open < level` (gap down).
3. Otherwise, a TP hit fills at the level, or at the **open** if `open > level` (gap up).
4. If nothing exits, `hwm = max(hwm, bar.high)` for the next bar. This is the same thing the app's `raise_highs` does
   after `check_exits`.
5. Then the RSI exit (`RSI > threshold`) and the MACD bearish cross, on the close, filled at the close.

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

**DCA last fallback:** if none of the presets in a regime's list is enabled but DCA is (e.g. only DCA enabled with
ACTIVE, whose lists never contain DCA), plain DCA runs at that regime's size multiplier, with "last fallback" in the
reason. Pause regimes (no list) still pause, and every `live_safe` gate and cap still applies.

BUY size = `BASE_SIZE_PCT[style]` (5 / 10 / 15 %) × multiplier, applied to `AutoConfig.capital`. SELL targets the
**oldest** position in the symbol (`position_id`, 100% of it, `price` = close). `exit_reason` maps each preset onto the
closest `ExitReason`, because there is no generic strategy-exit value:
- Buy the Dip → `RSI_EXIT`
- Trend → `MACD_EXIT`, or `TRAILING_STOP` when the close drops below EMA20 (EMA20 acts as the trailing line)
- Grid top cell → `TAKE_PROFIT`

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
| 7 | TP/SL/trailing checked on the close only; trailing → TP → SL priority | Intrabar high/low. Any hit stop (SL, trailing or max-loss) beats TP, and the highest hit stop wins. Trailing is strict (`low < trail`) and uses prior-bar highs only | Contracts v0.2; identical to Bot 3 |
| 8 | Exits fill at the close | Fill at the stop/target level, or at the open on a gap (SL/trailing: `open < level`; TP: `open > level`) | Same |
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
`pytest backend/engine` runs 196 tests:
- indicators against hand-computed values and independent pandas references
- regime detection on all mock shapes and 25 seeds per regime
- the full preset map and evaluate_auto
- evaluator gates and filters
- every exit rule in isolation and inside the backtest, plus a copy of Bot 6's exit-parity fixtures from `tests/e2e/test_exit_parity.py`
- backtest determinism and invariants, mark-to-market drawdown, fees and slippage, edge cases
- live/backtest consistency
- `test_real_data.py`: real-shaped candles (sparse sessions, weekend/overnight gaps, zero volume, bad rows, duplicates,
  unsorted input, tiny prices, 24/7 data with thin overnight/weekend liquidity)
- `test_live_safety.py`: every `live_safe` gate, the size caps, SELL never blocked, config validation
- `test_binance_samples.py`: REAL Binance responses (`tests/fixtures/binance/`, fetched Sun 11 Oct 2026 01:47 UTC
  from the public skill endpoints: NVDAB / NVDAon / TSLAx 1h K-lines, NVDAon status, rwa/dynamic): token families,
  the Ondo zero-volume fix, min-order floor / 25 USDT cap, market context (status, price impact)

## Live / mainnet (branch `bot2/live`)
The team now trades LIVE on BNB Chain mainnet (BNB Hack: Tokenized Stocks Edition) with real funds. The agent is
**fully autonomous and runs 24/7 with no per-trade approval**, so the engine has to cope with real on-chain data and
must never be the weak link. Simulator/backtest behaviour is unchanged unless noted.

### Real data handling (always on, every entry point)
Real Binance Web3 `market/candles` data for tokenized stocks (Bot 4, `backend/exchange/live.py`) is sparse and
irregular: hourly bars are missing off-hours and at weekends, DEX volume is often 0, and the API returns strings.
- `sanitize_candles()` runs first in `evaluate_manual`, `evaluate_auto`, `detect_regime` and `backtest_manual`. It:
  - drops `None` rows, missing or unparseable timestamps, NaN/inf/<=0 prices and `high < low`
  - widens high/low to contain open/close
  - turns volume `None`/NaN/negative into 0 (counted as missing)
  - keeps the LAST copy of a duplicated timestamp (a re-sent in-progress bar) and sorts by time
  - treats naive datetimes as UTC and accepts `Candle`, `model_construct` objects and dicts
  - fills in a `DataReport` whose issues are appended to signal reasons as `[data: ...]`
- **Warm-up:** live evaluation needs at least `warmup_bars + 1` (61) valid candles, the same rule as the backtest
  (which never trades before bar 60). Otherwise it returns `STAY_OUT` with `conditions_failed=["insufficient_data"]`
  and a reason like "Insufficient data: 40 valid candles, need at least 61".
- **Volume:** the volume condition and the min-volume filter need a real baseline: this bar traded, the average is
  > 0 and at least 50% of the bars in the window traded. Otherwise the condition is False and the filter fails with
  "no volume data". No divide-by-zero, and one stray trade among empty bars is never a "spike".
- **Regime:**
  - Dead bars (high == low and volume == 0, i.e. a closed or illiquid market) are ignored. If more than 50% of the
    last 20 bars are dead, the result is SIDEWAYS at confidence 0.2 with `features["insufficient_data"]=1`, so a
    flat closed-market stretch is never read as a trend.
  - Fewer than 61 active bars gives the same insufficient-data result.
  - Volatility uses only returns between contiguous bars (gap <= 1.5 bars), so overnight and weekend gaps are not
    counted as volatility.
  - Percent thresholds are multiplied by `clamp(sqrt(bar_hours), 0.25, 5)`. 1h is x1, 4h is x2 and 1d is about x4.9,
    so normal 3% daily moves are not HIGH_VOLATILITY on D1. RSI thresholds are not scaled.
  - The timeframe comes from `AutoConfig.timeframe` in auto mode, otherwise it is inferred from the bar spacing.
  - Price features keep full precision, so tiny prices work.
  - In `evaluate_auto`, an insufficient-data regime means STAY_OUT (strategy `NONE`) when nothing is held. When a
    position is held, it allows exits only.
- **DCA on sparse data:** a buy is due on the first bar at or after a slot boundary (`ts // (interval * bar)`), so a
  missing boundary bar does not skip a DCA. Regular data behaves exactly as before.
- 24/7 data works the same way. Thin overnight or weekend liquidity just produces more zero-volume or dead bars,
  which the rules above (and the `live_safe` liquidity gates below) handle.

### `live_safe=True`: conservative layer for PAPER / LIVE
```python
eng = DefaultStrategyEngine(live_safe=True, clock=lambda: datetime.now(timezone.utc))  # clock is injectable
eng = DefaultStrategyEngine(live_safe=True, live_config=LiveSafetyConfig(max_size_pct=1.0))
```
This layer only ever makes the engine more conservative. It never touches SELLs, because reducing exposure is always
allowed, even on stale data.

| `LiveSafetyConfig` field | Default | Effect on new BUYs (`conditions_failed` name) |
|---|---|---|
| `max_stale_bars` | 2.0 | Last candle opened more than 2 bars before `now` → blocked (`live_stale_data`) |
| `max_future_bars` | 1.0 | Last candle more than 1 bar in the future → blocked (`live_future_data`) |
| `require_volume` | True | Last bar volume 0 → blocked (`live_no_volume`) |
| `min_active_fraction` / `liquidity_lookback` | 0.25 / 20 | Fewer than 25% of the last 20 bars traded → blocked (`live_thin_liquidity`) |
| `thin_active_fraction`, `thin_relative_volume`, `thin_size_factor` | 0.6, 0.25, 0.5 | Fewer than 60% of bars traded, or the last bar is below 0.25x the average traded volume → size x0.5 |
| `require_all_conditions_ready` | True | Manual: any enabled entry indicator still warming up → STAY_OUT ("indicators still warming up") |
| `size_factor`, `max_size_pct` | 0.25, 2.0 | BUY size = min(proposed x 0.25, 2%), then x thin factor. Manual 15% → 2%; auto BALANCED 10% → 2%; PATIENT 5% → 1.25% |
| `market_hours` | **OFF** (`MarketHoursFilter(enabled=False)`) | Opt-in. When enabled, stock tokens (`…on`/`…x`/`…B`) only enter if both the last bar and `now` are inside the session (`live_market_hours`, `live_market_hours_now`). Off by default because the agent trades 24/7 and tokenized stocks may trade on-chain around the clock (Bot 4 is researching). |

- Blocked entries are `STAY_OUT` with `conditions_failed` set to the blocker names and the reason
  "Live safety blocked entry: …".
- BUY reasons end with "[live-safe size 10% x0.25 capped at 2%, thin liquidity (…) x0.5 -> 1%]".
- On a 1000 USDT account a live-safe BUY is at most 20 USDT, below Bot 3's hard caps (25 USDT per order, 25 USDT
  daily loss, 50 USDT exposure). Risk still enforces those caps.
- Token type uses Bot 4's suffix rule. `BNB`/`WBNB` end in `B` and are treated as bStocks, which only matters when
  market hours are enabled and errs on the safe side.

### What other bots must wire up
- **Bot 6 (loop):**
  - Before each `evaluate_*` call, `eng.set_market_context(symbol, {"status": <Bot 3 token status>,
    "reference_price_only": <bool>, "priceImpactPercent": <best route of a probe quote>, "equity": <wallet USDT>})`.
    Pass `None` when the data is unknown or stale.
  - In PAPER/LIVE, construct `DefaultStrategyEngine(live_safe=True, clock=<the loop's clock>)`. Keep the default
    engine for SIM and backtests.
  - Pass the `ManualStrategy.timeframe` / `AutoConfig.timeframe` that matches the candle interval actually fetched,
    because staleness and regime scaling use it.
  - Fetch at least 61 bars, and more when a strategy uses a long indicator period (for example SMA(200)).
  - Treat `STAY_OUT` with `live_*` or `insufficient_data` in `conditions_failed` as "no trade" and log the reason.
  - Always execute SELL signals.
- **Bot 3 (risk):** the hard USDT caps stay in risk. `live_safe` only reduces `proposed_size_pct` and
  `proposed_notional` before risk sees them. Because there is no per-trade user approval, risk's caps and kill
  switch are the binding limits.

### Token families, min order, market context (BNB Hack findings, Sun 11 Oct 2026)
`data.token_family(symbol)` → `"bstock"` (`…B`), `"ondo"` (`…on`), `"xstock"` (`…x`) or `"other"`.

| Family | What the real data shows | `live_safe` default |
|---|---|---|
| bStock | Trades 24/7 with real DEX volume (NVDAB: 48/48 weekend hourly bars traded) | Full volume rules (`live_no_volume`, `live_thin_liquidity`, thin size x0.5) |
| Ondo | Off-hours hourly bars are all volume `0`, ~0.2% range, price ≈ reference | Volume is **not applicable**: the `volume_above_avg` condition and min-volume filter are left out (not failed, not "warming up"), and the no-volume / thin-liquidity gates are skipped. Stale data is still refused. Min order **20 USDT** (Binance `40375`) |
| xStock | Zero liquidity on BSC (TSLAx bars weeks apart) | Entries blocked: `live_illiquid_family` |

The Ondo bug: before this change, Ondo's zero-volume candles made every live entry fail `live_no_volume` and
`live_thin_liquidity`, so Ondo could never be bought.

New `LiveSafetyConfig` fields:
- `blocked_families` (default `{"xstock"}`)
- `volume_exempt_families` (default `{"ondo"}`)
- `min_order_usdt` (default `{"ondo": 20.0}`; other families have no floor) and `bump_to_min_order` (default True)
- `max_order_usdt` (default 25.0)
- `equity_hint_usdt` (optional; sizes from min(equity, hint))
- `max_price_impact_pct` (default 1.0, the same limit Bot 3's risk applies)

**BUY sizing order:**
1. proposed % ×0.25, capped at 2%
2. thin-liquidity ×0.5 (volume families only)
3. notional on min(equity, hint)
4. below the minimum order → raise it to the floor, as risk does. The floor is the market context's
   `min_order_usdt` / `minOrderUsdt` when present, otherwise the family default. The engine only refuses
   (STAY_OUT `live_below_min_order`) when the floor can never be met: the floor > `max_order_usdt`, or a *known*
   equity (engine equity/capital > 0, context `equity`, or `equity_hint_usdt`) is below the floor. With unknown
   equity it buys at the floor (`proposed_size_pct` 0) and leaves the final say to risk.
5. Never above `max_order_usdt` (25 USDT).

`proposed_size_pct` is always `notional / equity × 100`.

**Market context (no contract change):** `eng.set_market_context(symbol, ctx)` or
`DefaultStrategyEngine(market_context={symbol: ctx})`. `ctx=None` clears it. Accepted keys:
- `status`: Bot 3's `trading` / `off-hours` / `closed` / `no liquidity`, also `open` / `paused`, or the raw Binance
  `statusInfo` keys `marketStatus` / `openState` / `reasonCode`. Status is case-, `-`-, `_`- and space-insensitive.
- `reference_price_only`
- `price_impact_pct`, or the Web3 quote's per-route `priceImpactPercent`. Both are percent units (e.g. `'0.35'` =
  0.35%), may be strings, and are parsed with `float()`. `null`, unparseable, NaN or inf = unknown: no block and no
  size change.
- `reference_price`
- `equity`
- `min_order_usdt` or `minOrderUsdt`: the token's minimum order (e.g. `get_token_status().min_order_usdt`).
  Strings are parsed with `float()`; `None`, unparseable or negative values are ignored. It overrides the family
  default, and `0` means no floor.

Effects on BUYs only (SELLs are never blocked):
- off-hours, closed, no liquidity, paused, `openState=false`, a reasonCode of ASSET_PAUSED / MARKET_PAUSED /
  MAINTENANCE, or `reference_price_only` → STAY_OUT `live_token_paused`
- `live_safe` only: price impact > `max_price_impact_pct` (1%) → STAY_OUT `live_price_impact`. There is no
  size-halving band, matching Bot 3, which rejects any live buy above 1% impact.

The token-status gate applies even without `live_safe`, because the loop only sets a context when it has real data.

### Fixtures
`tests/fixtures/binance/` holds real public samples (see Tests above). They only have 48 bars, so
`binance_samples.extended()` prepends rescaled copies of the real bars so there is enough warm-up history.
`/workspace/binance-check/samples/` (Bot 4's Web3 API dumps) did not exist at the time of writing.
`tests/real_fixtures.py` adds hand-made real-shaped data:
- string rows `[o, h, l, c, v, ts_ms, tradeCount]` parsed the way Bot 4 parses them
- NVDAon at about 230.88
- weekday 13:00–20:00 UTC sessions with missing bars and about 30% zero-volume bars
- weekend and overnight gap jumps
- dead closed-market bars
- a 24/7 series with thin overnight and weekend liquidity

When Bot 4 saves real samples, add a fixture test that loads them through `rows_to_candles`.
