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
  https://web3.binance.com/en/dev-portal/project). Ondo/BStock RFQ quotes also need `BINANCE_WALLET_ADDRESS`.
- **LIVE**: additionally `BINANCE_LIVE_ENABLED=true`, an executor (default: Agentic Wallet - `npm i -g @binance/agentic-wallet`,
  `baw auth signin`, confirm in the Binance App), each `OrderRequest.mode == LIVE`, and notional <= `BINANCE_LIVE_MAX_NOTIONAL` (default 25 USDT).
  There is **no testnet** for the Web3 Trading API - LIVE is mainnet.

### Behaviour callers must know (Bot 6)
- Dispatch on `quote.path`: `RFQ` -> `execute_rfq`, `SWAP` -> `execute_swap`. The wrong one returns `REJECTED`
  (real API: Ondo is always RFQ, BStock RFQ/SWAP, xStock SWAP).
- A quote is single-use and expires 30 s after creation (`EXPIRED`).
- `get_quote` in PAPER/LIVE raises `QuoteUnavailable` (with `.code`, e.g. 40367 market closed, 40375 < 20 USD,
  40374 no liquidity). SIM never raises for known tokens. Unknown symbols raise.
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
