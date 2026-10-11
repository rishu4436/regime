# Regime: BNB Hack Tokenized Stocks Edition submission

**Repo:** https://github.com/rishu4436/regime

## One line

Regime is an autonomous agent that reads the market regime of a tokenized stock and trades it 24/7 on BNB Chain
mainnet through the Binance Agentic Wallet, with hard safety limits and a plain-English reason for every decision.

## The problem

Tokenized stocks trade around the clock on-chain, but most people can't watch a chart all day, and most trading bots
are black boxes: they buy or sell without saying why, and nothing stops them from spending more than you meant. Regime
is built to be the opposite: it trades by itself, explains every decision in plain words, and can never go past the
limits you set.

## What it does

- **Reads the market.** From the last 60 usable candles, Regime puts the token in one of six regimes: strong uptrend,
  mild uptrend, sideways, oversold pullback, downtrend or high volatility.
- **Picks one strategy.** Your trading style (Patient, Balanced or Active) plus the regime selects exactly one of five
  built-in strategies: DCA, Smart DCA, Buy the Dip, Trend Following or Grid. In a downtrend or a wild market, the
  Balanced and Active styles stay out.
- **Or runs your own rules.** The Manual Builder combines RSI, SMA, EMA, MACD, Bollinger Bands, volume and breakouts
  with AND/OR, with take-profit, stop-loss, trailing-stop, RSI and MACD exits. The same engine runs your rules live
  and in the backtest, so both give the same result on the same candles.
- **Explains every decision.** Every Buy, Sell or Stay out comes with a reason naming the numbers behind it. The
  explanations come from built-in templates, or from xAI Grok if a key is set. The explainer only describes what
  happened and can never place a trade.
- **Checks every trade against hard limits.** The risk layer approves or blocks each live buy before it reaches the
  wallet and can always force Stay out. Stop-loss, take-profit and trailing-stop sells always go through.
- **Trades a watchlist.** The user can pick 1 to 5 tokens from the full list of about 680 tokenized stocks, or let
  Auto-pick choose them by theme (AI & chips, EV & energy, big tech, crypto, or any) using momentum, trend and how
  much they trade. Regime evaluates each one every tick, and the risk layer keeps all buys together within the caps.
- **Lets you stay in control.** The dashboard has Pause and an Emergency stop, plus optional confirmation mode if you
  want to approve each trade.

## How it uses BNB Chain and Binance

- **BNB Smart Chain mainnet.** Every live trade is a real swap on BSC that anyone can verify on BscScan.
- **Binance Agentic Wallet.** Regime places live orders through the Agentic Wallet CLI (`baw`). It never holds a
  private key: the user signs in by scanning a QR code from the Binance App, shown right on the dashboard. The
  wallet's own daily limit and abnormal-transaction setting apply on top of Regime's limits, and Regime uses
  whichever is stricter. Every swap is written to a local order journal before it's sent, so an unclear result is
  checked against the wallet's orders on the next tick instead of being retried blindly.
- **Binance Web3 API.** Signed requests give candles, prices, token status (trading, off-hours, no liquidity), each
  token's minimum order, and swap quotes with their price impact. Before every live buy, Regime stays out if the data
  is stale, the token is paused or closed, liquidity is too thin, or the quote moves the price more than 1% against
  it.
- **Tokens.** bStocks (`…B`, such as NVDAB) trade 24/7. Ondo tokens (`…on`) fill only during US market hours. xStocks
  (`…x`) are off by default because they have no BSC liquidity.
- **BNB Agent Studio.** Regime's ERC-8004 agent identity is prepared in `backend/agent_studio/`, and the dashboard
  has the steps to create its identity key, check it has BNB for gas, and register it. It is ready but **not
  registered on-chain yet**.
  <!-- If Rishu registers before submitting, replace the line above with the registration tx / agent id. -->

## Proof: autonomous trades on mainnet

**Headline:** on 11 Oct 2026 at 12:01 IST, Regime bought 10 USDT of NVDAB on its own with real funds, in Auto mode
with the Grid strategy.

**Transaction:** [0x187fdc3e1023e1ecd37af1dc7bed4176a13e75e39a0b35727681538c0690bf7f on BscScan](https://bscscan.com/tx/0x187fdc3e1023e1ecd37af1dc7bed4176a13e75e39a0b35727681538c0690bf7f)

**First trade:** Regime's first autonomous mainnet trade was also a 10 USDT NVDAB buy in Auto mode with Grid, at
09:25 IST the same day
([0x228c02688cd2a8170b38dba6abfdb4f95fca9e9e22ae6bed6a191d0b88a19f22 on BscScan](https://bscscan.com/tx/0x228c02688cd2a8170b38dba6abfdb4f95fca9e9e22ae6bed6a191d0b88a19f22)).
The reason it logged:

> Market regime sideways (Flat SMA50 (slope -0.12%) and low volatility (0.60%)); sideways market with the Balanced
> style selects Grid (preferred preset). Grid: price in the bottom grid cell 1 of 5 (1 = bottom, 5 = top) of range
> 229.2-235.5; size raised to the token minimum (10.00 USDT). Conditions met: price in the bottom grid cell. Live risk
> checks passed ($10.00).

Both trades were made by Regime on its own, with no human approving them.

## Safety

- **Three switches to go live:** `LIVE_TRADING_ENABLED`, `BINANCE_LIVE_ENABLED` and `RISK_ALLOW_LIVE` must all be
  true, or Regime can't spend real money.
- **Default live caps:** 25 USDT per order, 100 USDT bought per day, 50 USDT in open positions, and 25 USDT of daily
  loss, which triggers an emergency stop.
- **Adjustable while running:** the user can raise or lower the per-order, total, per-token, daily-loss and daily-buy
  limits at runtime. The new values reach the risk layer, the strategy engine and the exchange together, and none can
  go above the wallet's own daily limit.
- **Sized from the real wallet:** Auto mode sizes each buy from what the wallet actually holds, so a small wallet
  never gets an oversized order.
- **Exits always go through,** checked against each candle's high and low, not just its close.

## Architecture

A React + Vite dashboard talks to a FastAPI backend over REST and a live WebSocket. The backend runs a 24/7 loop that
first checks exits, then has the strategy engine read the regime and pick a strategy (or run manual rules), sends the
signal to the risk layer, executes approved trades through the exchange layer (SIM, PAPER or LIVE via the Agentic
Wallet), and has the explainer write up why. Every decision, risk check and execution is logged and shown on the
dashboard. The diagram is in the [README](../README.md#architecture).

## Try it

```bash
git clone https://github.com/rishu4436/regime.git
cd regime
backend/app/deploy/run.sh        # Windows: .\backend\app\deploy\run.ps1
```

Open http://localhost:5173. Regime starts in SIM mode with no keys, no wallet and no real money. Going live is
covered in [GO_LIVE.md](../backend/app/GO_LIVE.md).

## Links

- [README](../README.md)
- [Demo script](demo-script.md)
- [Developer Experience Report](dx-report/)
- [Going live with real funds](../backend/app/GO_LIVE.md)
- [API reference](../backend/app/API.md)
