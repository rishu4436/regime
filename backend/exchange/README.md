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
