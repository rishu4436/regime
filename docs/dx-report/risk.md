# Risk control

*Part of the Developer Experience Report for Regime by team `teamwork` (BNB Hack: Tokenized Stocks). Written 11 Oct 2026.
Every claim below points at code on `bot3/live` (`04e0944`, `backend/risk`) or at our research notes. Anything we did
not verify ourselves says so. The trade flow is in [ai-stack.md](ai-stack.md); exchange and `baw` details are in
[exchange.md](exchange.md); the loop, in-flight handling and e2e tests are in [integration.md](integration.md).*

## 1. Risk's role

In LIVE the agent is fully autonomous: it runs 24/7 and executes without asking per trade (`AUTO_EXECUTE`). So
`backend/risk` is the **last gate before real money moves**. It is deterministic Python with no network and no secrets.
Every `check()` returns a `RiskVerdict` with one named `RiskCheck` per rule, so each "Blocked (<check>): <detail>" or
"reduced from X to Y" can be shown and explained. Risk can always force *Stay Out*. Pause, resume and emergency stop
live here too, and the control state it uses is the most restrictive of its own state and the caller's copy, so a
stale copy can never un-pause the agent (`manager.py`).

## 2. The LIVE profile

`LiveRiskProfile` (`live.py`) applies automatically whenever the mode is LIVE. SIM and PAPER are unaffected.

| Limit | Default (env override) | Effect |
|---|---|---|
| Per-order cap | 25 USDT (`RISK_LIVE_MAX_ORDER_USDT`) | shrinks the order |
| Daily loss (realized today + unrealized) | 25 USDT (`RISK_LIVE_MAX_DAILY_LOSS_USDT`) | **auto emergency stop**: every position gets an `EMERGENCY_STOP` sell; only `reset_emergency_stop()` clears it |
| Daily notional (LIVE buys, UTC day) | 100 USDT (`RISK_LIVE_MAX_DAILY_NOTIONAL_USDT`) | shrinks the order |
| Open exposure | 50 USDT (`RISK_LIVE_MAX_EXPOSURE_USDT`) | shrinks the order |
| Slippage / price impact | 1 % (`RISK_LIVE_MAX_SLIPPAGE_PCT`) | blocks |
| Stale data | 900 s (`RISK_LIVE_MAX_DATA_AGE_S`) | blocks; missing timestamp also blocks |
| Minimum order | max(10 USDT, token minimum from Bot 4's `get_token_status`) | in LIVE the order is **raised** to it if every hard cap allows; else blocks |
| BNB gas reserve | 0.002 BNB (`RISK_LIVE_MIN_BNB`), plus a 1 USDT fee buffer | blocks / shrinks |

**Three switches, all required.** `LIVE_TRADING_ENABLED=true` (app), `BINANCE_LIVE_ENABLED=true` (exchange) and
`RiskConfig.allow_live`, which `RiskConfig.from_env()` sets only when `RISK_ALLOW_LIVE`, `LIVE_TRADING_ENABLED` and
`BINANCE_LIVE_ENABLED` are all exactly `true` (`config.py`). Otherwise every LIVE buy fails check `trading_mode`.

## 3. Size caps vs hard gates

- **Caps only shrink.** Cash, exposure, per-order, daily-notional and wallet caps each log "reduced from X to Y" and
  never increase an order. If the result falls under the minimum order, `min_order_notional` blocks the buy - except
  in LIVE (`bot3/live-fixes`): the order is **raised to the minimum** when it still fits every hard cap (per-order caps,
  wallet balance minus fee buffer, open-exposure room, daily notional room, wallet quota and exit reserve). The bump may
  override the %-of-equity size rule (15%) but never a hard cap; it's logged as "raised from X to Y USDT to meet the
  token minimum". Without it, a strategy-through-risk test showed 30 and 100 USDT wallets could never trade in LIVE.
  `live_limits()` reports `min_wallet_usdt` (minimum order + fee buffer) so the UI can warn "wallet too small".
- **Daily loss in LIVE** is governed by the absolute 25 USDT cap; the 5%-of-capital rule is skipped there
  (`use_pct_daily_loss_in_live=False`) and still applies in SIM/PAPER. Max open positions defaults to 3 unless a
  strategy sets it.
- **Hard gates block outright:** stale data, a bad or expired quote, price impact, token status, gas reserve, blocked
  symbols, pause and emergency stop.
- **Exits are never blocked.** Protective sells (stop-loss, take-profit, trailing, max-loss, emergency) pass every LIVE
  guard, never wait for confirmation, and are enforced even while paused. Nothing in the wallet-limit section touches
  sells either.

## 4. Safety layers (outermost first)

1. **Our limits** (section 2).
2. **The Agentic Wallet's own limits**, set in the Binance app and read with `WalletLimits.from_baw(...)`. Risk applies
   whichever is **stricter**, ours or the wallet's, so a buy is shrunk before the wallet would reject it.
   `live_limits()` reports the effective value and a `limit_source` of `"risk"` or `"wallet"`.
3. **Exit-quota reserve.** Assuming the wallet's `dailyLimit` also counts sells (section 6), check `wallet_exit_reserve`
   caps a buy at `(quota_left - open_exposure) / 2`, so the remaining quota can still sell every open position,
   including the new one. `live_limits()` warns `exit_quota_shortfall` when it can't, and `wallet_limits_stale` when the
   settings are over 15 min old.
4. **On chain:** the executor passes risk's cap as `baw --slippage` (default 1 %, never `auto`).
5. **Single flight:** the executor refuses a new swap while another is unresolved (`order_in_flight`).

## 5. Pending on-chain orders

A swap whose outcome isn't known yet is `PENDING_CONFIRMATION`, not a failure. For risk that means:

- `record_execution()` counts a pending buy toward **daily notional** at once, at its quote amount, keyed by
  `client_order_id`. The real fill replaces it, a failure releases it, and the same result is never counted twice
  (`04e0944`).
- The loop passes each pending buy to risk as a synthetic open position, so it counts toward **exposure** and blocks a
  second buy.
- Exits aimed at it are **held** (`pending_exit`) until the fill, then run on the real position. Mechanics are in
  [integration.md](integration.md).

## 6. Pain points, from the risk side

Exchange-side details are in [exchange.md](exchange.md). Here is only how each one changed risk decisions.

| # | What we hit | Effect on risk | Suggested fix (Binance / BNB Chain) |
|---|---|---|---|
| 1 | `baw` exposes **no per-transaction limit** and **doesn't return the allowed-token list** (only `tradeAllTokens`). The CLI can't set limits; they live only in the app. | Risk can't pre-check a token against the wallet's scope or mirror a per-tx cap, so it relies on its own per-order cap. `wallet_allowed_token` passes with a "not verified" note. | Return a per-tx cap and the allowlist in `baw wallet settings`, and offer a scoped, read-back way to set them. |
| 2 | **Unclear whether `dailyLimit` counts sells.** The docs never exempt them. An ended bStock campaign said bStock trades didn't consume the limit. | A stop-loss could be rejected once the quota is used up. We added the exit-quota reserve, which roughly halves buy capacity. Still unverified on mainnet. | Document it, or exempt sells (or at least exits) from the daily limit. |
| 3 | **Wallet limits are in USD**, our caps in USDT. | Treated 1:1. A stablecoin depeg would quietly skew the stricter-of-two comparison. | Report limits in the asset traded, or give the FX rate used. |
| 4 | **Ondo off-hours:** tradable on a short list of names, but zero-volume bars that behave like a reference price. | `live_token_status` blocks a buy when the token isn't `trading` or is `reference_price_only`. | One status field that says "reference price only" for each token. |
| 5 | **Ondo's 20 USD minimum** shows up only as error **40375**; no field gives it. | With a 25 USDT cap, wallet shrinking can push an Ondo buy below 20. Risk uses `max(10, token_status.min_order_usdt)`; the token value comes from Bot 4's `get_token_status`, whose 20 for Ondo is itself inferred from the error text. | Add `minOrderUsd` to token or market status. |
| 6 | **`priceImpactPercent`** comes as a string (`"0.0001"`), isn't always present, and its sign isn't documented. | Risk takes it as an optional float (`LiveContext.price_impact_pct`) and counts its magnitude as adverse. Without it, risk compares the quote with the reference price by direction: a worse price is capped at 1 %, and a better one is allowed up to a 3 % sanity cap. | Always return a numeric field, and document the sign (we suggest positive = worse for the trader). |
| 7 | **Market hours vs 24/7:** bStock trades 24/7, Ondo 24/5 plus off-hours, and US hours shift with EDT/EST. | We switched the US-hours guard **off by default** (`7f33dd9`) and gate on each token's own status instead. Holidays were never modelled. | A per-asset `isTradableNow` flag next to the session. |
| 8 | **Candle `timestamp` is the bar's open time.** | A 1h bar's open is always more than 900 s old, so a naive stale-data check blocks everything. Risk uses the fetch time, or `bar.timestamp + bar_interval` (the close). | Include the close time (or an `isClosed` flag) in candle rows. |
| 9 | **Gaps in our own contracts:** `RiskManager.check` has no `context`, `Portfolio` has no wallet balances, `Quote` has no price impact. | We added an optional `context: LiveContext` (balances, wallet limits, token status, price impact, data time) as a Bot 3 extension. | For a hackathon starter kit: put balances, impact and token status in the shared models from day one. |

What worked well: the official `wallet settings` reference let us map `dailyLimit` / `quotaUsed` / `quotaLeft` onto our
caps directly. Error codes 40367/40369/40374/40375 were distinct enough to act as market-state signals.

## 7. Test evidence (run 11 Oct 2026, ~07:50 IST)

- `backend/risk` (branch `bot3/live` @ `04e0944`):
  `uv run --with-requirements requirements.txt --with pytest python -m pytest -q -p no:warnings backend/risk` →
  **179 passed**. That covers the checks, intrabar exits, controls, the LIVE profile and switches, wallet limits, the
  exit reserve and pending-order accounting.
- Full-system and e2e numbers are in [integration.md](integration.md).
