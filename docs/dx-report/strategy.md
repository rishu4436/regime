# Strategy engine

*Part of the Developer Experience Report for team `teamwork` (BNB Hack: Tokenized Stocks). Written 11 Oct 2026.
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
| Exits | `exits.py` | Intrabar stop loss, take profit, trailing stop and max loss, with rules identical to Bot 3's `backend/risk/exits.py`. |
| Backtest | `backtest.py` | The same evaluator code run over history. Mark-to-market equity, optional fees and slippage, deterministic output. |
| Real-data layer | `data.py` | Cleans candles, infers the timeframe, detects gaps and dead bars, classifies token families. |
| Live safety | `live.py` | `live_safe=True` gates and sizes for PAPER/LIVE. It only ever makes the engine more conservative. |

**One code path.** Live signals and backtests use the same indicator and rule code.
`test_backtest_entries_match_live_evaluator_on_prefix` checks that every backtest entry matches what the live evaluator
says on that prefix of bars.

### Exit rules shared with risk

Bot 3 (risk) and Bot 2 (engine) agreed on one set of exit semantics, and Bot 6's `tests/e2e/test_exit_parity.py` runs
both implementations on the same fixtures:

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
  - per-family minimum order (Ondo 20 USDT, see §3)
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

## 3. Real mainnet data: findings and pain points

On Sun 11 Oct 2026 we saved real responses from Binance's public skill endpoints (`bot2-notes/skills_research.md`).
The engine's tests now load them (`backend/engine/tests/fixtures/binance/`: NVDAB, NVDAon and TSLAx 1h K-lines, the
NVDAon status, and `rwa/dynamic`).

**The three issuers behave very differently on BSC, and nothing in the APIs says so up front.**

| Family | What we measured (Sunday, off-hours) | What the engine does |
|---|---|---|
| bStock (`…B`, e.g. NVDAB) | Asset status `TRADING` with no session. 26 weekend hourly bars carried ~$579k of DEX volume, with an average hourly high-low range of ~1.62%. | Full volume rules. This is the 24/7 demo family. |
| Ondo (`…on`, e.g. NVDAon) | Status `offhours`, `openState=true`. All 48 hourly bars have **volume `0`**, with ~0.20% average range; the price behaves like a reference/oracle price. | Volume conditions are "not applicable" (skipped, not failed), and the no-volume / thin-liquidity gates are skipped. Stale-data checks still apply. |
| xStock (`…x`, e.g. TSLAx) | Liquidity `0`, 24h buy/sell volume 0. Its 48 most recent hourly bars span 15 Aug → 4 Oct. Bot 4 got `40374` (insufficient liquidity) on a Sunday quote. | Entries blocked by default (`live_illiquid_family`), configurable. |

| # | Pain point | Impact on the engine | Suggested fix |
|---|---|---|---|
| 1 | **Zero-volume Ondo candles off-hours.** The K-line volume field is `"0"` on every bar. | Our first live-safety version blocked **every** Ondo buy (no volume, thin liquidity). A volume-based rule can never fire. We found and fixed this only with the real sample. | Document the volume semantics per issuer and session, or return `null` instead of `0` when there is no DEX trading. |
| 2 | **Liquidity differs per issuer, but status still says `TRADING`.** TSLAx is `TRADING` with zero liquidity. | Status alone can't tell a strategy whether a token is tradable. We added token-family rules based on the symbol suffix. | Add a liquidity / tradability flag to the market-status response. |
| 3 | **The shares multiplier changes the price.** NVDAon raw price 230.870 vs stock 230.475 looks like +0.17%, but after dividing by `sharesMultiplier` (1.0017…) the premium is 0.00%. | Any premium/discount or cross-issuer spread signal is wrong unless every price is divided by the multiplier. | Return a multiplier-adjusted per-share price next to the token price. |
| 4 | **The `offhours` status is undocumented.** The market-status endpoint returns an `offhours{openState, nextOpenTime, nextCloseTime}` block and `marketStatus: "offhours"` that the skill doc (v1.1) doesn't describe. Its meaning is our inference. | We can't be sure if an Ondo token is buyable off-hours. In the end the team treated off-hours as "no new buys". | Document every `marketStatus` and `reasonCode` value and what each one allows. |
| 5 | **Ondo has a 20 USD minimum order (`40375`), but our live sizes are small.** The live sizing is ×0.25, capped at 2% of equity, under a 25 USDT per-order cap. | On small accounts, Ondo buys come out below the minimum and the exchange rejects them. The engine now raises the order to the 20 USDT floor only when that is ≤ the 25 USDT cap and ≤ equity. Otherwise it stays out with `live_below_min_order`. | Expose `min_order_usd` per token in the token/RWA info, so clients can size correctly before quoting. |
| 6 | **`volume24h` is US stock volume, not DEX volume.** `rwa/dynamic.tokenInfo.volume24h` and the rank list's `volume24h` (~$19.5B for NVDAon) are stock-market figures. | Using them as on-chain liquidity would be badly wrong. We use candle volume and the quote's price impact instead. | Rename the field, or add a separate `dexVolume24h`. |
| 7 | **The smart-money signal feed is memecoin-only.** `trading-signal` returned 40 BSC signals, all memecoins and none for stock tokens. | It can't be used as an entry condition for stock tokens. | A stock-token filter or category on the signal endpoint. |
| 8 | **Token audit doesn't cover bStocks.** `query-token-audit` for NVDAB returns `hasResult=false, isSupported=false`. | We can't add an audit/risk gate for the token we trade most. | Audit coverage for the RWA tokens Binance lists. |
| 9 | **The signed Web3 API is geo-blocked.** Every HMAC call from our US-hosted box returned `40304` (compliance restriction). | Real candles and quotes could only be fetched from the team lead's PC, so the engine was hardened first on hand-built fixtures and then on public-endpoint samples. | A region-free sandbox, or a clear HTTP 451, plus documented equivalence between the public K-line and `/market/candles`. |

Two smaller findings:
- **Different K-line shapes.** The public skill K-line rows are `[openTime, o, h, l, c, volume, closeTime]`, while the
  Web3 API documents `[o, h, l, c, v, ts, tradeCount]`, and prices and volumes arrive as strings. The engine's
  `sanitize_candles()` accepts either after parsing: it drops NaN/≤0/high<low rows, de-duplicates, sorts, and treats
  naive times as UTC.
- **Sparse hourly bars off-hours** (Bot 4's observation) used to look like volatility and broke clock-based DCA. Regime
  volatility now uses only returns between consecutive bars, and DCA fires on the first bar after a schedule boundary.

## 4. Engine/risk parity on price impact

Risk (`backend/risk`, `live_slippage`) blocks a live buy when the exchange-reported price impact is above its
`max_slippage_pct` (1% by default in LIVE). The engine has to agree, or it would propose buys that risk always rejects.
So `LiveSafetyConfig.max_price_impact_pct` defaults to **1.0**, and an impact above it gives STAY_OUT
`live_price_impact`. An earlier "halve the size between 1% and 2%" band was removed for the same reason.

- **Same input.** Both read the Web3 quote's per-route `priceImpactPercent` in percent units. Bot 4 passes it as a
  string (e.g. `'0.35'` = 0.35%). The engine parses it with `float()`, and `null` or unparseable means *unknown*, which
  neither blocks nor resizes.
- **Known difference.** Risk compares `abs(impact)`, while the engine compares the signed value. A negative impact
  beyond −1% passes the engine but is blocked by risk. Risk stays the binding limit. Aligning this is a one-line
  change we haven't made yet.

## 5. Test evidence (branch `bot2/live` @ `4b2749b`, run 11 Oct 2026, Python 3.13)

- `pytest backend/engine`: **196 passed**. This includes indicators against hand-computed and pandas references,
  regime detection on seeded mock series, every exit rule, backtest determinism, real-shaped sparse/24/7 data, every
  `live_safe` gate, and the real Binance samples.
- Full repo `pytest` on that branch: **211 passed**. The contracts check is up to date and ruff is clean.
- Bot 6's exit-parity e2e test passed earlier (Phase 2) against the real engine and risk code.

What worked well: the public skill endpoints worked keyless from our box, so the real samples behind §3 could be
fetched and turned into regression tests within minutes. And the regime → one-preset design kept Auto mode easy to
explain: every signal names its regime, its preset and the rule that fired.
