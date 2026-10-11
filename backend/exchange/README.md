# `backend/exchange` — Bot 4 — Binance Web3 API Layer

**Owner:** Bot 4 — Binance Web3 API Layer. This bot edits only this directory.

## Scope (verbatim from the spec PDF)
**Goal:** Handle all Binance Web3 API and Agentic Wallet interaction.

- HMAC authentication and signed requests
- Market data (candles, prices, RWA search)
- Quote handling with ~30s TTL
- SWAP execution path and RFQ execution path
- Simulation mode that mimics real calls
- Clean interfaces for the rest of the system

## Interface it must implement
Implement `backend.contracts.ExchangeClient`:

```python
mode -> TradingMode                                   # property
get_candles(symbol, timeframe=Timeframe.H1, limit=200) -> list[Candle]   # oldest first
get_price(symbol) -> float
search_tokens(query) -> list[TokenInfo]               # RWA / tokenized stock search
get_quote(symbol, side, notional) -> Quote            # expires_at = created_at + ~30s
execute_swap(order: OrderRequest, quote: Quote) -> ExecutionResult
execute_rfq(order: OrderRequest, quote: Quote) -> ExecutionResult   # expired quote -> status EXPIRED, never raise
```
Provide a SIM client (mimics real calls/latency/TTL, no network), a PAPER client (real market data, simulated fills)
and a LIVE client (signed HMAC requests + Agentic Wallet) — LIVE must be opt-in only and clearly separated.
`factory.get_exchange_client(mode)` picks the client from `TRADING_MODE`. Credentials only from env vars
`BINANCE_API_KEY` / `BINANCE_API_SECRET`; never log them. HTTP tests must use recorded/mocked responses.

## Implementation (Bot 4)

| File | What |
|---|---|
| `factory.py` | `get_exchange_client(mode=None, executor=None)` - SIM (default) / PAPER / LIVE from env (see docstring) |
| `sim.py` | `SimulatedBinanceClient` - offline; mocks' candles/prices + real API rules (routing, 30 s TTL, path check, Ondo $20 min, optional market-hours gate, latency) |
| `live.py` | `BinanceWeb3Client` - PAPER (real data + quotes, simulated fills) and LIVE (executor, per-order cap) |
| `web3_api.py` | signed HTTP client for `https://web3.binance.com/build` (candles, prices, RWA search, quote, swap, approve, RFQ submit/status, broadcast, tx detail) |
| `signing.py` | HMAC-SHA256 -> Base64 over `timestamp + METHOD + /build/path?query + body` |
| `executors.py` | LIVE back-ends: `AgenticWalletExecutor` (`baw` CLI, MPC keyless) and `Web3ApiExecutor` (own `WalletSigner`) |
| `tokens.py` | default token registry (real BSC contracts for NVDAon/TSLAx/AAPLB/SPYon/METAx) + unit helpers |

### Modes
- **SIM** (default, no network, no keys). Same symbols and deterministic prices as `backend.mocks`.
- **PAPER**: `TRADING_MODE=PAPER` + `BINANCE_API_KEY`/`BINANCE_API_SECRET` (Web3 Developer Portal keys,
  https://web3.binance.com/en/dev-portal/project) **and `BINANCE_WALLET_ADDRESS`**: xStock quotes are RFQ and the API
  rejects them without `userWalletAddress` (40001 "userWalletAddress is required for RFQ (xStock) quote"); the client
  fails fast with `QuoteUnavailable(code=40001)` instead. Ondo/BStock RFQ routes need it too. A quote-only wallet
  (unfunded, never used to sign) is enough for PAPER. Note the API is region-gated: from a US IP every call returns
  40304 "Service not available due to compliance restriction".
- **LIVE**: additionally `BINANCE_LIVE_ENABLED=true`, an executor (default: Agentic Wallet - `npm i -g @binance/agentic-wallet`,
  `baw auth signin`, confirm in the Binance App), each `OrderRequest.mode == LIVE`, and notional <= `BINANCE_LIVE_MAX_NOTIONAL` (default 25 USDT).
  There is **no testnet** for the Web3 Trading API - LIVE is mainnet.

### Behaviour callers must know (Bot 6)
- Dispatch on `quote.path`: `RFQ` -> `execute_rfq`, `SWAP` -> `execute_swap`. The wrong one returns `REJECTED`
  SIM routing: Ondo and xStock -> `RFQ`, BStock -> `SWAP`. PAPER/LIVE use the route's real `executionMode`
  (verified 2026-10-11, read-only: xStock needs the RFQ wallet; 25 USDT -> NVDAon and -> NVDAB both came back as
  `SWAP` routes via the DEX aggregator on a Sunday, while TSLAx returned 40374 insufficient liquidity).
- SIM replay: `sim.set_replay_index(i)` pins time to bar `i` of the `sim.n_bars(symbol)`-bar series (candles up to i, price = bar i close, quotes/fills at that price; clamped; `None` = default). `sim.replay_index` reads it.
- A quote is single-use and expires 30 s after creation (`EXPIRED`).
- `get_quote` in PAPER/LIVE raises `QuoteUnavailable` (with `.code`, e.g. 40367 market closed, 40375 < 20 USD,
  40374 no liquidity, 40001 xStock without wallet). SIM never raises for known tokens. Unknown symbols raise.
  With `enforce_market_hours=True` SIM rejects out-of-hours fills: Ondo 40367, BStock 40369, xStock 40374.
- LIVE results that could not be confirmed in time come back `FAILED` with `error` starting `UNCONFIRMED` and a
  `tx_hash`/`order_id` to reconcile (the contract has no PENDING status).

### Sources
- Binance Web3 API docs: https://web3.binance.com/en/dev-docs/llms.txt (index), `/authentication`,
  `/products/trading-api/introduction`, `/products/trading-api/integration-flow`, `/products/trading-api/error-codes`,
  `/products/market-api/introduction`
- Official connector (param names): https://github.com/binance/binance-web3-connector-python
- Agentic Wallet: https://web3.binance.com/en/dev-docs/products/agentic-wallet/introduction,
  https://github.com/binance/binance-skills-hub/tree/main/skills/binance-web3/binance-agentic-wallet
- Tokenized-stock list: https://github.com/binance/binance-skills-hub/blob/main/skills/binance-web3/binance-tokenized-securities-info/SKILL.md

## Definition of done
- Implements the interface above exactly (signatures from `backend/contracts/interfaces.py`, data shapes from `backend/contracts/models.py`). `isinstance(obj, <Protocol>)` is True.
- Unit tests live in this directory's `tests/` and pass with `pytest` from the repo root; the whole suite stays green.
- Works fully in **SIM** mode with `backend.mocks` (no network, no API keys).
- No edits outside the owned directory. Contract changes go through the coordinator (see CONTRIBUTING.md).

## Token status & non-exception quotes (bot4/token-status)
Extras beside the contract (`backend/exchange/status.py`; contracts unchanged). Available on `SimulatedBinanceClient`
and `BinanceWeb3Client` (PAPER/LIVE):

- `get_token_status(symbol) -> TokenStatus` with `trading_status` (`TRADING | OFFHOURS | CLOSED | NO_LIQUIDITY | UNKNOWN`),
  `tradable`, `reason`, `min_order_usdt` (Ondo 20, else 10), `max_order_usdt`, `shares_multiplier`, `reference_price`,
  `price`, raw `market_status` / `reason_code` / `reason_msg`, `next_open_time`, `next_close_time`, `source`; `.to_dict()`.
- `quote_or_reason(symbol, side, notional) -> QuoteResult` never raises for market state: `ok`, `quote` (contract Quote),
  `reason` (`market_closed | insufficient_liquidity | below_minimum | xstock_disabled | above_maximum`), `message`,
  `error_code`, `trading_status`, `min_order_usdt`, `price_impact_pct`, `reference_price`, `shares_multiplier`,
  `token_status`; `.to_dict()`. Auth/config errors still raise. SELLs are never blocked by the minimum.
- `quote_meta(quote_id) -> QuoteResult | None` for quotes made with plain `get_quote`.
- `QuoteUnavailable.reason` carries the same reason code (40367/40369 -> market_closed, 40374 -> insufficient_liquidity,
  40375 -> below_minimum, 40366 -> above_maximum).
- xStock (`…x`) is disabled by default: `BINANCE_ENABLE_XSTOCK=true` re-enables it.
- SIM: bStock TRADING 24/7, Ondo OFFHOURS Sat/Sun UTC, xStock NO_LIQUIDITY; `set_trading_status()` forces one.

## LIVE Agentic Wallet executor (bot4/baw-live)
Real funds on BSC through the Binance Agentic Wallet CLI (`baw` 1.10.0, `npm i -g @binance/agentic-wallet@1.10.0`).
Built from `baw --help` + the CLI's bundled source; tests use a fake runner with the exact `--json` shapes (no network).

| File | What |
|---|---|
| `baw.py` | `BawCli` subprocess wrapper (`--json`, errors = JSON on stdout + exit 1 -> `BawError(reason)` / `BawSessionExpired`), 20 s reads / 45 s swap, `resolve_baw_command()` (`BAW_CMD`, else on Windows `node %APPDATA%\npm\node_modules\@binance\agentic-wallet\dist\index.js`), reason codes, `classify()` |
| `baw_executor.py` | `BawExecutor` (LiveExecutor + wallet reads), `BawLiveClient` (Web3 data/quotes/status + baw execution/wallet), `IntentStore`, `live_gate()`, `result_reason()`, `wallet_snapshot()` |
| `wallet.py` | `get_balances()` / `get_wallet()` / `get_wallet_settings()` on `BawLiveClient` (LIVE) and `SimulatedBinanceClient` (SIM) |
| `baw_smoke.py` | `python -m backend.exchange.baw_smoke [--json]` - READ-ONLY checks (status, address, balance, settings, left-quota, tx-lock, quote 1 USDT->NVDAB). Never swaps (read_only client refuses writes before spawning). |

**Gate (per order):** `LIVE_TRADING_ENABLED`, `BINANCE_LIVE_ENABLED`, `RISK_ALLOW_LIVE` must all be `true`; BUY notional
<= `BINANCE_LIVE_MAX_ORDER_USDT` (25), SELL <= `BINANCE_LIVE_MAX_SELL_USDT` (2x). Every swap/quote passes
`--slippage <BINANCE_LIVE_MAX_SLIPPAGE_PCT>` (default 1 %, never `auto`) or `execute_order(..., max_slippage_pct=)`; `--mev true`.

**One order:** process lock -> reconcile/refuse open intents -> `wallet status` CONNECTED -> `wallet tx-lock` UNLOCKED ->
no PENDING `market-order list` -> intent persisted (`BAW_INTENT_FILE`, default `~/.teamwork/baw_intents.json`) ->
`market-order swap` -> poll `market-order list --orderId` every 3 s up to 120 s -> FINISHED: fill + gas from
`wallet tx-history` (instructions.send/receive + networkFee) | FAILED | PENDING -> `PENDING_CONFIRMATION`.
A timed-out/ambiguous submit is never retried: reconciled via `market-order list --fromToken --toToken --startTime`,
else `PENDING_CONFIRMATION` + `outcome_unknown` with the intent kept open (new swaps refused until
`executor.reconcile()` or `executor.resolve_intent(id)`). Note: `tx-history --tx` only returns status in 1.10.0, so
amounts come from the `tx-history` list entry with that hash.

**Results:** non-filled results carry `error="[<reason>] ..."` (`result_reason(res)`); extras (no contract fields):
`executor.extras[client_order_id]` = `{explorer_url, route (ONDO_RFQ|SWAP), real_funds: True, wallet_order_id, reason,
slippage_pct, gas_fee_bnb, gas_fee_usd, fill_source}`. Reasons: `session_expired`, `wallet_not_ready` (pause + alert:
`PAUSE_REASONS`), `live_disabled`, `order_cap_exceeded`, `tx_locked`, `order_in_flight`, `app_confirmation_required`,
`abnormal_txn_rejected`, `quota_exceeded`, `policy_rejected`, `insufficient_balance`, `insufficient_gas`,
`slippage_exceeded`, `no_route`, `market_closed`, `amount_out_of_range`, `quote_expired`, `service_unavailable`,
`baw_timeout`, `baw_not_found`, `outcome_unknown`, `pending_confirmation`, `order_failed`, `order_rejected`, `baw_error`.

## Dashboard sign-in (`wallet_connect.py`)

`WalletConnect` backs the dashboard's "Connect wallet" button. `start()` runs `baw auth signin --json` (returns at once
with `urlForWeb` = QR link, `qrCodeId`, `pairingCode`) and launches `baw auth verify --qrCodeId <id> --json` in the
background (baw polls for up to ~5 min). `poll(session_id)` returns `{status: pending|connected|expired|failed, address,
wallet_changed, error}`. On `connected` it has checked `baw wallet status` = CONNECTED and read the BSC address.
`wallet_changed` is true when that address differs from `BINANCE_WALLET_ADDRESS` (or the `expected_address` the app passes,
i.e. the wallet the saved LIVE state belongs to), so the app keeps LIVE blocked until positions are reconciled. One sign-in
at a time; a new `start()` cancels the old one. The HTTP endpoints must be localhost-only: anyone who can call them can
start a sign-in for this machine's wallet.


## Paid second opinion over x402 (`x402.py`, bot4/x402)

`StockAgentX402` pays BNB Chain's Stock Analyze Agent (`https://stock-agent.bnbchain.org`) about 0.1 U per report
with the Agentic Wallet (`baw x402-payment preview` / `sign`) and polls the async job. **It never trades**: its `BawCli`
is allow-listed to `wallet status`, `wallet settings` and `x402-payment preview|sign`. Reports take 2–5 min, so never
call it from the trading loop's hot path.

**Env:** `STOCK_AGENT_X402_ENABLED` (default `false`), `STOCK_AGENT_X402_MAX_PRICE` (0.1, per call), `STOCK_AGENT_X402_DAILY_CALLS`
(5 per UTC day, counted in `$STATE_DIR/x402_state.json`, else `LOG_DIR`, else `~/.teamwork`), `STOCK_AGENT_X402_USDT_RESERVE`
(0: USDT kept for exits/fees if the payment would be in USDT), `STOCK_AGENT_X402_PAY_TO` (pinned payee, default the one the
service returned on 2026-10-11), `STOCK_AGENT_X402_URL`.

**Flow of `analyze(symbol, guard=, emergency_stop=, wallet_blocked=, usdt_reserve=)`:** enabled -> `wallet status` CONNECTED ->
guard -> daily cap -> unpaid `POST /x402/analyze/async {"symbols":["NVDA"],"analysis_type":"comprehensive"}` = 402 ->
`baw x402-payment preview --paymentRequirements <base64 PAYMENT-REQUIRED header> --json` -> pick a READY_TO_SIGN option
(prefers U, USD1, then USDC/USDT; refuses Permit2 options that need an approve tx, and any payee other than the pinned one)
-> amount (highest of preview amount, amountUsd and the 402's wei amount) <= cap -> `x402QuotaLeft` >= amount -> balance /
USDT reserve -> guard again with the price -> `baw x402-payment sign --paymentId <id> --selectedIndex <n> --json` (the only
spend) -> replay with `PAYMENT-SIGNATURE` (503 settlement_pending replays the **same** proof) -> `jobId` + `jobToken` written
to the state file at once (if that write fails the error message carries both values). Symbols are mapped to the
underlying ticker (NVDAB/NVDAon -> NVDA, TSLAx -> TSLA).

The x402 quota (`x402DailyLimit` $20/day) is **separate** from the trading `dailyLimit`: `baw wallet settings` reads them
from separate fields (`x402QuotaUsed` vs `quotaUsed`), so a paid call doesn't eat into the quota exits need. It does
spend a token balance, which is why U/USD1 are preferred and USDT payments respect `usdt_reserve`.

**Refusal reasons** (`X402Error.reason`; all raised before signing unless noted): `x402_disabled`, `session_expired`,
`guard_blocked`, `daily_cap_reached`, `price_over_cap`, `x402_quota_exceeded`, `exit_reserve`, `insufficient_balance`,
`no_signable_option`, `approve_required`, `pay_to_mismatch`, `bad_symbol`, `rate_limited`, `unexpected_response`,
`service_unavailable`; after signing: `payment_failed` (proof kept as `pending`; `resume_pending()` replays it, never
re-signs), `state_write_failed`.

After a paid call, `client.limits_dirty` is `True` and the `on_paid(result)` hook fires: re-read wallet limits/balances,
then `clear_limits_dirty()`.

**API for Bot 6 to wire (Bot 1's contract):**

| Endpoint | Client call | Returns |
|---|---|---|
| `GET /api/x402/price?symbol=` | `client.price(symbol)` | `{enabled, price_usdt, cap_usdt, calls_left_today, quota_usdt_left \| null}` (never raises; nulls when the service or wallet can't be read) |
| `GET /api/x402/status` (optional) | `client.status()` | the above + `{cap, calls_today, limits_dirty}` |
| `POST /api/x402/analyze {symbol}` | `client.analyze(symbol, guard=..., emergency_stop=..., wallet_blocked=...)` | `{job_id, paid_usdt, tx_hash \| null}`; `X402Error` -> 4xx with `err.to_dict()` (`{reason, message, data?}`) |
| `GET /api/x402/jobs/{id}` | `client.job(id)` | `{status: "pending" \| "done" \| "failed", report: markdown \| null, error \| null}`; unknown id -> `job_not_found` (404) |

Keep these endpoints localhost-only like the wallet ones: `analyze` spends wallet money.
