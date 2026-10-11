# Strategy engine

*Part of the Developer Experience Report for **Regime**, our autonomous tokenized-stock agent (BNB Hack: Tokenized Stocks). Written 11 Oct 2026.
Every claim below points at code in `backend/engine` (branch `bot2/live` @ `4b2749b`) or at our research notes. Anything
we did not verify ourselves says so.*

## 1. What the engine does

`backend/engine` is the deterministic "brain" of the agent. It turns candles and a strategy config into exactly one
`Signal` per call (**BUY / SELL / STAY_OUT**), with a plain reason and the names of the conditions that were met or
failed. It does not place orders, apply hard risk limits or call an LLM. Risk (`backend/risk`) can always block or
shrink a signal, and the explainer (`backend/llm`) only puts it into words.

| Piece | File | What it does |
|---|---|---|
| Indicators | `indicators.py` | Causal SMA, EMA, Wilder RSI, MACD (true 9-EMA signal), Bollinger(20, 2σ), rolling volume average, ATR, rolling max/min, crosses. Value *i* depends only on bars 0..*i*. Missing values are `None` and are always tested with `is None`. |
| Regime detector | `regime.py` | Six regimes, checked in order (first match wins): HIGH_VOLATILITY, OVERSOLD_PULLBACK, DOWNTREND, STRONG_UPTREND, MILD_UPTREND, SIDEWAYS. Returns a confidence, a reason and the features it used. |
| Presets and Auto mode | `presets.py` | Five presets (DCA, Smart DCA, Buy the Dip, Trend, Grid). A regime × trader-style table (PATIENT / BALANCED / ACTIVE) picks **one** preset, or "pause". Only that preset is evaluated. |
| Manual evaluator | `manual.py`, `engine.py` | User-built rules: AND/OR entry conditions, hard filters (min volume, market hours), RSI/MACD exits, cooldown, max open positions. |
| Exits | `exits.py` | Intrabar stop loss, take profit, trailing stop and max loss, with rules identical to the risk module's `backend/risk/exits.py`. |
| Backtest | `backtest.py` | The same evaluator code run over history. Mark-to-market equity, optional fees and slippage, deterministic output. |
| Real-data layer | `data.py` | Cleans candles, infers the timeframe, detects gaps and dead bars, classifies token families. |
| Live safety | `live.py` | `live_safe=True` gates and sizes for PAPER/LIVE. It only ever makes the engine more conservative. |

**One code path.** Live signals and backtests use the same indicator and rule code.
`test_backtest_entries_match_live_evaluator_on_prefix` checks that every backtest entry matches what the live evaluator
says on that prefix of bars.

### Exit rules shared with risk

The engine and risk use one set of exit semantics (parity test: [integration.md](integration.md)):

- Stops are tested against the bar's low and TP against the high.
- Any stop that is hit beats TP, and the highest stop that was hit wins. Exact ties go trailing > SL > max-loss.
- The trailing stop is strict (`low < trail`) and uses only highs from *prior* bars.
- Fills happen at the level, or at the open on a gap.

### Deterministic backtest

- The loop starts at bar 60, the same as the reference simulator.
- It never enters on the last bar.
- It produces one mark-to-market equity point per candle.
- Fees and slippage are optional (both 0 by default, for parity with the simulator).
- Open positions are closed at the end with reason `END`.
- The same inputs give an identical `BacktestResult`: there are no random ids or clocks in it.

### `live_safe` layer (PAPER / LIVE)

`DefaultStrategyEngine(live_safe=True, clock=...)` keeps the strategy logic and adds BUY-only gates. **SELLs are never
blocked**, because reducing exposure is always allowed.

- **Data checks:**
  - stale data: last candle older than 2 bars against an injectable `now` → `live_stale_data`
  - candle from the future → `live_future_data`
  - last bar with no volume → `live_no_volume`, or fewer than 25% of the last 20 bars traded → `live_thin_liquidity`
  - fewer than 61 valid candles → `insufficient_data`
  - a manual indicator still warming up → STAY_OUT
- **Sizing:**
  - proposed % × 0.25, capped at 2% of equity
  - × 0.5 on thin-but-tradeable liquidity
  - per-family minimum order (Ondo 20 USDT, see §3.3)
  - never above `max_order_usdt` (25 USDT, the same as risk's live per-order cap)
- **Optional market context:** `set_market_context(symbol, ctx)`. No contract change was needed.
  - token status off-hours / closed / no liquidity / paused, or `reference_price_only` → `live_token_paused`
  - quote price impact above 1% → `live_price_impact`
- **Market-hours gate:** off by default, because the agent trades 24/7 and bStocks trade on weekends. It can be
  switched on for stock tokens.

## 2. Bugs we fixed relative to the reference simulator

The organisers' reference (`full_strategy_builder_simulation.html`) was the starting spec. Studying it
(`bot2-notes/reference_study.md`) turned up these problems, all fixed in the engine:

| # | Reference simulator | Engine |
|---|---|---|
| 1 | RSI is seeded with **sums** and then smoothed as if they were averages; `loss \|\| 1` hides the no-loss case | Standard Wilder RSI; 100 when there are no losses, 50 on a flat series. The old formula is kept as `rsi_simulator`, and a test shows where the two differ |
| 2 | MACD signal uses alpha 0.15 | A true 9-period EMA (alpha 0.2), seeded with the SMA of the first 9 MACD values |
| 3 | Truthiness checks (`v && ...`) treat a value of exactly 0 as missing | `is None` checks |
| 4 | Volume average over the whole series (look-ahead) | Rolling 20-bar average |
| 5 | `minVol` is mixed into the entry conditions, so under OR it can *trigger* a buy | Min volume is a hard filter, always ANDed |
| 6 | Market hours faked as `bar_index % 24 < 16` | Real UTC hours from candle timestamps (and off by default in live) |
| 7 | TP/SL checked on the close only | Intrabar high/low with a defined tie rule (§1) |
| 8 | `continue` skips `equity.push`; equity is realised-only | One mark-to-market point per bar, so drawdown is no longer understated |
| 9 | No fees or slippage | `fee_pct` / `slippage_bps` |
| 10 | `Math.random` candles | Seeded mock candles, so backtests are reproducible |
| 11 | Can enter on the last bar and close it immediately as `End` | No entry on the last bar |

Because of 1, 2, 4 and 7, the engine deliberately does **not** reproduce the simulator's trade list bar for bar.

## 3. Real mainnet data: what broke the engine, and what would help

On Sun 11 Oct 2026 we saved real responses from Binance's public skill endpoints (`bot2-notes/skills_research.md`).
The engine's tests now load them (`backend/engine/tests/fixtures/binance/`: NVDAB, NVDAon and TSLAx 1h K-lines, the
NVDAon status, and `rwa/dynamic`).

| Family | What the candles look like (Sunday, off-hours) | What the engine does |
|---|---|---|
| bStock (`…B`, e.g. NVDAB) | All 48 hourly bars have DEX volume. 26 weekend bars carried ~$579k, with an average hourly range of ~1.62%. | Full volume rules |
| Ondo (`…on`, e.g. NVDAon) | All 48 hourly bars have volume `"0"`, with ~0.20% average range, so the price tracks the reference | Volume is "not applicable" (§3.1) |
| xStock (`…x`, e.g. TSLAx) | Its 48 most recent hourly bars span 15 Aug → 4 Oct | Entries blocked by default (`live_illiquid_family`) |

### 3.1 Engine-specific pain points

| # | Pain point | Impact on the engine | Suggested fix |
|---|---|---|---|
| 1 | **Zero-volume Ondo candles off-hours.** The K-line volume field is `"0"` on every bar, not `null`. | `volume_above_avg` and the min-volume filter can never pass, and our first live-safety version blocked **every** Ondo buy (no volume, thin liquidity). We found this only with the real sample. The fix: per-family volume rules. For Ondo, volume conditions are skipped rather than failed (and don't count as "warming up"). For other families, a volume comparison now needs a real baseline (this bar traded, and at least half the bars in the window did), so one stray trade among empty bars is never a "spike". | Return `null` (or a flag) when there was no DEX trading, and document volume semantics per issuer and session. |
| 2 | **Flat or empty bars confuse the regime detector.** Closed-market placeholder bars (high = low, volume 0) and gaps between sessions are not "price action". | A flat stretch after a rally could still read as an uptrend, and weekend or overnight gaps inflated volatility into HIGH_VOLATILITY. Now: if more than half of the last 20 bars are dead, the result is low-confidence SIDEWAYS (`insufficient_data`). Dead bars are excluded, and volatility uses only returns between consecutive bars. | A per-bar `tradeCount` / "no trading" flag on every K-line source (the Web3 API rows have `tradeCount`; the public K-line rows don't). |
| 3 | **The shares multiplier changes the price.** NVDAon raw price 230.870 vs stock 230.475 looks like +0.17%, but after dividing by `sharesMultiplier` (1.0017…) the premium is 0.00%. | Any premium/discount, reference-price or bStock-vs-Ondo spread rule is wrong unless every token price is divided by the multiplier first. The engine has no premium rule yet; any future one must divide by the multiplier first. | Return a multiplier-adjusted per-share price next to the token price. |
| 4 | **`volume24h` is US stock volume, not DEX volume.** `rwa/dynamic.tokenInfo.volume24h` and the rank list's `volume24h` (~$19.5B for NVDAon) are stock-market figures. | They look like a liquidity number, but they would massively overstate on-chain liquidity. The engine uses only candle volume, plus quote price impact via the market context. | Rename the field, or add a separate `dexVolume24h`. |
| 5 | **Smart-money signals and token audit don't cover stock tokens.** `trading-signal` returned 40 BSC signals, all memecoins. `query-token-audit` for NVDAB returns `hasResult=false, isSupported=false`. | Neither can be an entry condition or a risk filter for the tokens we trade, so the engine uses neither. | A stock-token category on the signal feed; audit coverage for listed RWA tokens. |
| 6 | **Two K-line shapes, all strings.** The public skill K-line is `[openTime, o, h, l, c, volume, closeTime]`; the Web3 API documents `[o, h, l, c, v, ts, tradeCount]`. | Index mix-ups are easy. Both are parsed into `Candle`, then `sanitize_candles()` cleans them (§3.2). | One documented row schema, or keyed objects. |

### 3.2 Warm-up, bar length and data cleaning

Real candles are sparse, irregular and sometimes malformed, which mock candles never are. These rules apply on every
entry point (`evaluate_manual`, `evaluate_auto`, `detect_regime`, `backtest_manual`):

- **Cleaning (`sanitize_candles`):**
  - drops `None` rows, missing or unparseable timestamps, NaN/inf/≤0 prices and `high < low`
  - widens high/low to contain open/close
  - sets missing or negative volume to 0
  - keeps the last copy of a duplicated timestamp (a re-sent in-progress bar) and sorts
  - treats naive times as UTC
  
  Problems found are appended to the signal reason as `[data: ...]`.
- **Warm-up:** live evaluation needs at least 61 valid candles, the same rule as the backtest (which never trades
  before bar 60). Otherwise it returns STAY_OUT `insufficient_data` with the count, for example "40 valid candles, need
  at least 61". The real public samples have only 48 bars, so the tests prepend rescaled copies of the real bars.
- **Bar length:** the timeframe comes from the strategy config, or is inferred from the typical bar spacing (robust to
  gaps). Regime percent thresholds are scaled by `√(bar hours)`, clamped to 0.25–5, so a normal 3% daily move is not
  "high volatility" on 1d bars. Clock-based DCA fires on the first bar after a schedule boundary, so a missing boundary
  bar no longer skips a buy.
- **Stale data:** in `live_safe`, a last candle older than 2 bars against an injectable `now` blocks new buys.
  SELLs are never blocked.

### 3.3 Covered elsewhere (engine angle only)

- **Signed Web3 API geo-block (`40304`), no testnet, `baw` issues:** real candles could only be fetched from the team
  lead's PC, so the engine was hardened on hand-built fixtures first and public-endpoint samples second. See
  [exchange.md](exchange.md).
- **xStock liquidity:** status says `TRADING` while liquidity is 0, so the engine blocks xStock buys by family. See
  [exchange.md](exchange.md).
- **Undocumented `offhours` status:** the engine treats off-hours / closed / no liquidity / paused as "no new buys"
  (`live_token_paused`). See [exchange.md](exchange.md).
- **Ondo 20 USD minimum (`40375`):** live sizes are small, so the engine raises an Ondo buy to 20 USDT when that is
  within the 25 USDT cap and equity, and otherwise returns `live_below_min_order`. See [exchange.md](exchange.md).
- **Caps, wallet limits, slippage:** the engine only shrinks: ×0.25 sizing, capped at 2% of equity and at 25 USDT.
  Risk's caps are binding. See [risk.md](risk.md).
- **Exit parity and integration tests:** the engine and risk share one exit rule set, checked by the App/Integration e2e parity
  test. See [integration.md](integration.md).

## 4. Engine/risk parity on price impact

Risk blocks a live buy whose exchange-reported price impact is above 1% (`max_slippage_pct`; see [risk.md](risk.md)).
`LiveSafetyConfig.max_price_impact_pct` defaults to the same 1.0, so the engine never proposes a buy that risk will
reject. Above it, the engine returns STAY_OUT `live_price_impact`. An earlier "halve the size between 1% and 2%" band
was removed for the same reason.

- **Same input.** Both read the Web3 quote's `priceImpactPercent` in percent units. A string such as `'0.35'` is
  parsed with `float()`, and `null` or unparseable means unknown: no block and no resize.
- **Known difference.** Risk compares `abs(impact)`, while the engine compares the signed value. Risk stays the binding
  limit.

## 5. Test evidence (branch `bot2/live` @ `4b2749b`, run 11 Oct 2026, Python 3.13)

- `pytest backend/engine`: **196 passed**. This includes indicators against hand-computed and pandas references,
  regime detection on seeded mock series, every exit rule, backtest determinism, real-shaped sparse/24/7 data, every
  `live_safe` gate, and the real Binance samples.
- Full repo `pytest` on that branch: **211 passed**. The contracts check is up to date and ruff is clean.

What worked well: the public skill endpoints worked keyless from our box, so the real samples behind §3 could be
fetched and turned into regression tests within minutes. And the regime → one-preset design kept Auto mode easy to
explain: every signal names its regime, its preset and the rule that fired.
