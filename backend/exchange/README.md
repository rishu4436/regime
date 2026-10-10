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

## Definition of done
- Implements the interface above exactly (signatures from `backend/contracts/interfaces.py`, data shapes from `backend/contracts/models.py`). `isinstance(obj, <Protocol>)` is True.
- Unit tests live in this directory's `tests/` and pass with `pytest` from the repo root; the whole suite stays green.
- Works fully in **SIM** mode with `backend.mocks` (no network, no API keys).
- No edits outside the owned directory. Contract changes go through the coordinator (see CONTRIBUTING.md).
