# AI stack

*Part of the Developer Experience Report for team `teamwork` (BNB Hack: Tokenized Stocks). Written 11 Oct 2026.
Every claim below points at code in this repo or at our research notes. Anything we did not verify ourselves says so.*

## 1. What the AI does, and what it is not allowed to do

The agent trades tokenized stocks (bStocks such as NVDAB, Ondo `…on` tokens) on BNB Smart Chain mainnet. Each loop tick
runs: **control check → regime → strategy → risk → execute → explain → log** (`backend/app`).

| Layer | What it is | Decides trades? |
|---|---|---|
| Strategy & regime engine (`backend/engine`) | Deterministic Python. Indicators (RSI, SMA, EMA, MACD, Bollinger, volume, breakouts), a regime detector (strong/mild uptrend, range, pullback, choppy, downtrend), preset agents (DCA, Smart DCA, Buy the Dip, Trend, Grid). Only one strategy is active in Auto Mode. | Proposes a signal with reason codes |
| Risk layer (`backend/risk`) | Deterministic. Size and exposure caps, stop-loss, trailing stop, cooldowns, daily loss cap, pause and emergency stop. It can always force *Stay Out*. | Can block or shrink any signal |
| Executor (`backend/exchange`) | Binance **Agentic Wallet** through the `baw` 1.10.0 CLI in LIVE, a simulator in SIM/paper. | Executes only what risk allowed |
| **LLM explainer (`backend/llm`)** | xAI **Grok**, with a deterministic template fallback | **No.** It only writes text |
| Identity (`backend/agent_studio`) | BNB Agent Studio SDK `bnbagent` 0.5.0, ERC-8004 identity | No |

We chose this split on purpose. Trading decisions must be reproducible, testable and auditable, so no LLM output sits
on the money path. The LLM makes the agent *understandable*. It explains every buy, sell and stay-out, summarises the
regime and the active strategy, answers status questions, and turns plain English into a strategy config.

## 2. The LLM layer: Grok with a template fallback

- **Interface.** `DefaultExplainer` implements the shared `Explainer` protocol: `explain_decision`, `summarize_state`,
  `parse_strategy_from_text` and `answer` (`backend/llm/explainer.py`).
- **Model.** `GrokClient` calls the OpenAI-compatible `POST {XAI_BASE_URL}/chat/completions` with httpx.
  `XAI_API_KEY` turns it on. Defaults are `XAI_MODEL=grok-4.7`, `XAI_BASE_URL=https://api.x.ai/v1`, a 20 s timeout and
  temperature 0.2 (0.0 for parsing). The key is never logged and is masked in `repr` (`client.py`).
- **Fallback.** Grok is tried first. On any error, timeout, empty reply or invalid output, the explainer uses
  deterministic templates (`templates.py`), and `last_source` records `"grok"` or `"template"`. With no key, or with
  `LLM_DISABLED=1`, everything runs offline. So tests, CI and the demo never depend on the LLM being reachable.
- **Isolation.** The module has no access to the exchange client. A static test checks that it imports nothing from the
  exchange, risk, app, engine or mocks code. The system prompt says the model never places, changes or cancels trades and never
  claims it did (`prompts.py`). Facts reach the model only as JSON of our contract models.
- **Natural language to strategy.** Grok returns `{"kind": "manual"|"auto", "strategy": {...}}`. `parser.py` pulls out the
  JSON, drops unknown keys and invalid fields, and validates the result against the pydantic contracts. If that fails,
  a keyword parser takes over. Parsed strategies always use `activation=CONFIRMATION` unless the user's own text asks
  for automatic execution. IDs, timestamps and versions supplied by the LLM are ignored.

### LIVE-mode guardrails (`backend/llm/live.py`)

In LIVE the agent trades on its own with real funds, so the explanation has to say exactly that:

1. **No approval wording.** If Grok's text matches the approval pattern ("confirm…", "await…", "your approval",
   "pending approval", …), it is rejected and the template text is used instead. Approval phrases that come in from
   upstream detail strings are rewritten too (for example "approved" becomes "allowed").
2. **Tx link enforcement.** Every fill names the tx hash and a BscScan link. The explainer accepts a supplied
   `explorer_url` only if it starts with `https://bscscan.com/tx/`. Otherwise it rebuilds the link from a real 66-character
   tx hash. If Grok leaves the link out, the template sentence carrying it is appended.
3. **Honest statuses.** `PENDING_CONFIRMATION` reads as "submitted on-chain, waiting to settle", never as "bought". A
   missing price impact reads "Price impact unknown". Failed, expired or blocked exits are never described as sold.

### One vocabulary for reasons across engine, risk and exchange

Three teams emit reasons in three formats: engine codes (`live_*`, `insufficient_data`), risk strings
(`"Blocked (<check>): <detail>"`), and Binance Web3 / `baw` error codes. `live.py` maps all of them to plain English.
It keeps the numbers and says whether **the agent's risk limits** or **the Agentic Wallet** set the cap. Examples:
40367/40369 (market closed), 40374 (no liquidity), 40375 (Ondo 20 USD minimum), 40401 (quote expired),
`session_expired`, `order_in_flight` and `pending_exit` ("the exit is held because the buy is still settling on-chain;
it runs as soon as the fill is confirmed"). Unknown reasons pass through cleaned up, and `live.handled_codes()` lists
every mapped code so the integration tests can check coverage.

## 3. Binance Agentic Wallet as the executor

The LIVE executor runs the `baw` CLI as a subprocess. It always uses `--json`, polls each `orderId` until it finishes,
writes an intent journal before every swap, and never retries a swap whose outcome is unknown. Only one swap is in
flight at a time. It reads real fills from `wallet tx-history`, and it passes risk's slippage cap as `--slippage`
(never `auto`). These rules come from the `binance-agentic-wallet` skill in the Binance Skills Hub.

**Two layers of limits.** The wallet's own spending controls, which are set only in the Binance app, sit *under* our
risk caps. The agent trades under the **lower** of the wallet's `dailyLimit`/`quotaLeft` and its own `RISK_LIVE_*` caps
(defaults: 25 USDT per order, 100 USDT per day, 50 USDT exposure, 1 % slippage). A second, independent executor cap
(`BINANCE_LIVE_MAX_ORDER_USDT=25`) applies as well. Risk keeps part of the daily quota in reserve so every open position
can still be sold (`backend/app/GO_LIVE.md`; Bot 4 research §§2–3).

## 4. BNB Agent Studio: on-chain identity

`backend/agent_studio` registers the agent as an **ERC-8004 identity** on BSC with the Python SDK **`bnbagent` 0.5.0**:

- `register.py` is a one-off CLI. Testnet is the default, and a mainnet run needs `--network mainnet --confirm-mainnet`.
  It sends `register(uri, metadata)` and then `setAgentURI`. The owner is a **separate small EVM key** read only from
  `AGENT_STUDIO_PRIVATE_KEY`, which is never printed or saved. That key never holds trading funds. The script refuses
  to run if the key's address equals the trading wallet's address.
- Registries (the SDK presets): mainnet `0x8004A169FB4a3325136EB29fA0ceB6D2e539a432` (chain 56), testnet
  `0x8004A818BFB912233c491871b3d84c89A494BD9e` (chain 97). The agent page is on `https://8004scan.io/agents/bsc/<id>`,
  and the registration tx is on BscScan.
- The Agentic Wallet's public address can be stored as `trading_wallet` metadata, so judges can link identity and wallet.
  `get_agent_identity()` feeds `/api/system → agent_identity` without network calls.
- Gas: a read-only `eth_estimateGas` gave about 957k gas for `register` at a 0.1 gwei floor, so about 0.0002 BNB for
  both txs. We fund the key with 0.002 BNB for headroom.
- **Status:** the code and tests are done. The mainnet registration itself still has to be run from the team lead's PC.

**x402 idea (not implemented, not verified end to end).** BNB Chain's Stock Analyze Agent
(`https://stock-agent.bnbchain.org`, about 0.1 U per call, paid from the wallet's separate $20/day x402 quota via
`baw x402-payment preview/sign`) could act as a slow, paid "second opinion" inside explanations. Reports take 2–5 min, so
it would never be on the trading hot path. `X402_NOTES.md` documents the flow. No code spends money on it.

## 5. Developer-experience friction (and what would help)

| # | Friction we hit | Impact | Suggestion |
|---|---|---|---|
| 1 | **Geo-blocking.** Every Binance Web3 API call from our US-hosted build box returned **40304** "Service not available due to compliance restriction" (with HTTP 200). `baw` is blocked the same way. | All live checks and the agent itself had to run on the team lead's PC in India. Cloud CI cannot do live smoke tests. | A sandbox/testnet endpoint without a region gate, or a clear HTTP 451 status. |
| 2 | **`baw` sign-in is a QR scan, and the session can end silently** (max 48 h, plus sign-out after inactivity). | A "24/7 autonomous" agent needs a human to re-scan. We added auto-pause, a `WALLET_SESSION_EXPIRED` alert and plain-language explanations. | A long-lived, revocable agent credential scoped by the app's spend limits, plus an expiry warning before the session ends. |
| 3 | **`abnormalTxnHandling` must be changed by hand to `AutoReject`.** The docs recommend app confirmation for high-risk trades (`NeedConfirmation`). | A flagged trade waits up to 5 min for a tap and **tx-locks the wallet**, which blocks stop-loss sells too. Our setup guide makes AutoReject a required step. | Default to AutoReject for agent wallets, or don't lock exits behind a pending review. |
| 4 | **No per-transaction limit, and the allowed-token list can't be read back.** Only the `tradeAllTokens` boolean is exposed. | The agent can't check a token before quoting it; out-of-scope tokens fail only at submit. We enforce our own per-order cap. | Expose a per-tx cap and the allowlist in `baw wallet settings`. |
| 5 | **`baw market-order quote` JSON has no quoteId, fee, gas or price impact**, and `market-order list` has no received amount. | Price impact comes from a second API (Web3 `/aggregator/quote`), and the real fill comes from `tx-history`. | Return price impact, a reference price and fill amounts in the CLI JSON. |
| 6 | **Unclear whether sells consume `dailyLimit`.** The docs don't say. | We reserve quota for exits. A stop-loss could otherwise be refused. | Document it, or exempt exits. |
| 7 | **The CLI has no HTTP timeout**, and errors go to stdout with exit code 1. On Windows, `baw.cmd` doesn't resolve without a shell. | The caller must add timeouts and parse stdout first. `run.ps1` invokes `node …/dist/index.js` directly. | A `--timeout` flag; errors on stderr. |
| 8 | **Agent Studio's managed runtime is testnet-only** (48 h trial), has **no Agentic Wallet wallet kind**, and the `bag` scaffold is a TypeScript *seller* agent. | Our mainnet trading loop can't run in Studio's cloud. We use the Python SDK only for identity. | A `baw` wallet kind and a mainnet runtime; a Python trading-agent template. |
| 9 | **The SDK's default testnet RPC** (`data-seed-prebsc-2-s2.binance.org:8545`) timed out from our box. | We had to override the RPC (`bsc-testnet-rpc.publicnode.com` worked). | Default to a more reliable RPC, or try several. |
| 10 | **`stock-agent.bnbchain.org` is slow.** Our check of `GET /x402/price` at 07:44 IST took 15.4 s (HTTP 200). | Too slow for a live loop. It is only usable as an async second opinion. | Publish latency expectations and a cached price endpoint. |
| 11 | **xStock "TRADING" status is not the same as liquid.** A TSLAx quote returned 40374 on Sunday. Off-hours candles are sparse. | xStock is off by default in LIVE. NVDAB (bStock) is the 24/7 demo token. | Add a liquidity flag to the market-status endpoint. |

What worked well: the Skills Hub's wallet skill gave us clear, safe execution rules. The keyless RWA market-status
endpoints made "is this token tradable right now?" cheap to check. `bnbagent` was small and easy to read, and its
MegaFuel paymaster usually makes testnet registration gas-free.

## 6. Test evidence (run 11 Oct 2026, ~07:50 IST, Python 3.13)

- `backend/llm` (branch `bot5/live` @ `65815aa`): **158 passed**.
- `backend/agent_studio` (`e190f24`): **28 passed, 1 skipped**. The skipped test checks against the real `bnbagent` SDK
  and needs the package installed.
- Full integration build `integ-v8` @ `8a268d9`: **887 passed, 1 skipped**.
