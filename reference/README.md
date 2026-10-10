# Reference: Manual Strategy Simulator

`full_strategy_builder_simulation.html` is the working simulator named in spec §2.3. Open it in a browser (no server needed).
It is the **functional reference for Bot 1 (UI)** and for **Bot 2's manual rule evaluator / backtest**.
`backend/contracts/models.py` (`ManualStrategy` and its sub-models) mirrors its fields, defaults and semantics.

## Field mapping (simulator DOM id → contract field → default)

| Simulator | Contract (`ManualStrategy.…`) | Default |
|---|---|---|
| `token` (NVDAon / TSLAx / AAPLB) | `symbol` | `NVDAon` |
| `useRSI`, `rsiEntry` (RSI period fixed 14) | `entry.rsi_below.{enabled, threshold, period}` | `true`, `35`, `14` |
| `useSMA`, `smaPeriod` | `entry.price_above_sma.{enabled, period}` | `true`, `50` |
| `useEMA`, `emaPeriod` | `entry.price_above_ema.{enabled, period}` | `false`, `20` |
| `useMACD` (12/26/≈9) | `entry.macd_bullish_cross.{enabled, params}` | `false`, `12/26/9` |
| `useBB` (20, 2σ, `close < lower*1.02`) | `entry.bollinger_lower.{enabled, period, std_dev, proximity_pct}` | `false`, `20`, `2.0`, `2.0` |
| `useVolume` (`volume > avg*1.1`) | `entry.volume_above_avg.{enabled, period, multiplier}` | `false`, `null` (= whole window), `1.1` |
| — (PDF only) | `entry.breakout.{enabled, lookback, direction}` | `false`, `20`, `HIGH` |
| `entryLogic` | `entry.logic` | `AND` |
| `tp` | `exit.take_profit_pct` | `12` |
| `sl` | `exit.stop_loss_pct` | `6` |
| `useTrail`, `trail` | `exit.trailing_stop.{enabled, pct}` | `false`, `4` |
| `useRsiExit`, `rsiExit` | `exit.rsi_above.{enabled, threshold}` | `true`, `70` |
| `useMacdExit` | `exit.macd_bearish_cross.enabled` | `false` |
| `size` | `risk.position_size_pct` | `15` |
| `maxPos` (UI 1–3) | `risk.max_open_positions` | `1` |
| `cooldown` | `risk.cooldown_bars` | `5` |
| — (PDF only) | `risk.max_loss_per_trade_pct` | `null` |
| `marketHours` | `filters.market_hours.enabled` | `false` |
| `minVol` | `filters.min_volume.{enabled, multiplier}` | `false`, `1.1` |
| `capital` | `backtest_manual(initial_capital=…)` / `DEFAULT_INITIAL_CAPITAL` | `10000` |
| loop starts at bar 60 | `SIM_WARMUP_BARS` | `60` |

Exit reasons map to `ExitReason`: `Trailing Stop`→`TRAILING_STOP`, `Take Profit`→`TAKE_PROFIT`, `Stop Loss`→`STOP_LOSS`,
`RSI Exit`→`RSI_EXIT`, `MACD Exit`→`MACD_EXIT`, `End`→`END`. Mock start prices match (NVDAon 140, TSLAx 200, AAPLB 170).

## Simulator semantics that the engine must reproduce
- **Bar loop:** for each bar from index 60: first manage exits, then consider an entry. Prices are the bar **close**.
- **Exits, first match wins:** trailing stop (`close < trail`; trail starts at `entry*(1-pct)` and ratchets to `close*(1-pct)`)
  → take profit (`pnl% >= tp`) → stop loss (`pnl% <= -sl`) → RSI exit (`RSI > level`) → MACD bearish cross
  (`line < signal` now and `line > signal` on the previous bar).
- **Entry gate:** `open_positions < max_open_positions` AND `bar - last_exit_bar > cooldown_bars` AND market-hours filter.
- **AND/OR:** each enabled condition whose indicator is available contributes one boolean; conditions still in warm-up
  (indicator null) are **skipped**, not counted false. No booleans → no entry. AND = all true, OR = any true.
- **MACD bullish cross:** `line[i] > signal[i]` and `line[i-1] <= signal[i-1]`.
- **Sizing / PnL:** entry size = `capital * size%` (current capital), PnL = `size * pnl%`, applied to capital only on exit;
  no fees/slippage. Remaining positions are closed at the last close with reason `End`.
- **Stats:** return %, win rate over SELLs, number of SELLs, max drawdown over the (realised) equity curve, final capital.
- **Indicators:** SMA of closes; EMA seeded by SMA, `k = 2/(n+1)`; MACD = EMA12 − EMA26, signal = EMA with α=0.15 seeded
  by the first MACD value; Bollinger = SMA20 ± 2·population σ; RSI(14) Wilder-style (see quirks below).

## Mismatches / quirks vs the PDF (decisions taken in the contracts)
1. **Price-action breakout** (PDF §2.1 "break of recent high/low") is **not in the simulator**. Added as `entry.breakout` (disabled by default).
2. **Max loss per trade** (PDF §2.1) is **not in the simulator**. Added as `risk.max_loss_per_trade_pct` (optional); enforced by Bot 3.
3. **Minimum volume / liquidity filter:** the PDF describes a minimum volume/liquidity filter; the simulator's `minVol`
   ("Require above-average volume") just pushes the same `volume > avg*1.1` boolean into the entry conditions — so under
   **OR** logic it can *trigger* an entry instead of filtering. **Contract decision:** `filters.min_volume` is a **hard gate**
   (always ANDed), with an extra optional absolute `min_volume`. This intentionally deviates from the simulator under OR logic.
4. **Market hours:** the simulator fakes it as `bar_index % 24 < 16`. The contract uses real UTC hours
   (`start_hour_utc=13`, `end_hour_utc=20`, `weekdays_only=true`, ≈ US session). Backtests on mock data use candle timestamps.
5. **Volume average uses the whole series** in the simulator (look-ahead bias in a backtest). Contract keeps it as the
   default (`period: null` = average of all candles passed in) for parity; set `period` (e.g. 20) for a rolling, look-ahead-free average.
6. **MACD signal** uses α=0.15 (≈ 12.3-period EMA, not a true 9-period α=0.2) and is seeded with the first MACD value;
   `params` exposes 12/26/9 but the reference evaluator should use the simulator's α to match results.
7. **RSI quirk:** the simulator seeds with *sums* of gains/losses over 14 bars, then applies Wilder smoothing as if they were
   averages, and uses `loss || 1` to avoid divide-by-zero. This distorts early RSI values. Bot 2 should implement standard
   Wilder RSI and add a parity test documenting the difference, or flag a "simulator-compat" option internally.
8. **RSI / Bollinger periods** are fixed in the simulator UI (14, 20, 2σ); the contract makes them configurable with the same defaults.
9. **Activation:** the PDF's "Manual or Confirmation mode" is `ManualStrategy.activation` = `AUTO_EXECUTE` | `CONFIRMATION`
   (default `CONFIRMATION`); the simulator has no activation (backtest only).
10. **Tokens:** simulator offers NVDAon/TSLAx/AAPLB only; the PDF also mentions token search with price/type/chain (Bot 4 `search_tokens`).
11. **Fees/slippage:** none in the simulator; real/SIM execution via Bot 4 includes them, so live PnL will differ from backtests.
