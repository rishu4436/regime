# Backend API (Bot 6) — REST + WebSocket

Base URL `http://localhost:8000`. The Vite dev server proxies `/api`, `/health` and `/ws`.
All bodies are JSON. Every model name below is a contract model from `backend/contracts`
(TypeScript: `frontend/src/contracts.ts`). Timestamps are ISO-8601 UTC. Errors: `{"detail": "..."}` with
4xx status (`404` unknown id/symbol, `409` invalid state such as "no active agent", `422` validation error).

Contract version **0.2.0**. `SystemInfo`, `LoopState`, `BacktestRequest`, `Alert` (+ `AlertLevel`) and
`PerformanceSummary` are contract models (in `contracts.ts`). Small request bodies (`{reason}`, `{text}`, ...) are
app-only and defined in `backend/app/schemas.py`.

## System
| Method | Path | Body | Response |
|---|---|---|---|
| GET | `/health` | – | `{status, contracts_version, trading_mode}` |
| GET | `/api/system` | – | `SystemInfo`: `{trading_mode, live_enabled, contracts_version, sim_seed, components: {engine, risk, exchange, explainer}}` (`components.*` = class name, or `"fallback:<name>"` while a bot's module is still a stub) |

## Market data
In SIM, everything here is "as of" the loop's current replay bar (before the first tick: the end of the 60-bar
warm-up): candles end at that bar, prices are its close, the regime preview uses no later bars. The loop pins Bot 4's
simulated client with `set_replay_index(i)` each tick and the app also trims, so no future bars ever leak to the UI.
History is separate: in SIM the full seeded series per symbol/timeframe is built once at start-up and cached;
`POST /api/backtest` and `GET /api/candles/{symbol}?history=true` read that cache, so they never move as the loop advances.

| Method | Path | Body | Response |
|---|---|---|---|
| GET | `/api/tokens?q=` | – | `TokenInfo[]` + `token_status` per token (Bot 4 `get_token_status` as JSON: `trading_status`, `min_order_usdt`, `reference_price_only`, ...; `null` if unavailable; cached ~60 s) |
| GET | `/api/candles/{symbol}?timeframe=1h&limit=200` | – | `Candle[]` (oldest first), replay-pinned in SIM (ends at the loop's current bar), last `limit` (default 200) |
| GET | `/api/candles/{symbol}?history=true[&limit=N]` | – | `Candle[]` full history: SIM = the cached seeded series (`n_bars` candles, unchanged by ticks; the same bars `/api/backtest` uses), PAPER/LIVE = the exchange's candles (default 500). `limit` = last N; omit for all |
| GET | `/api/price/{symbol}` | – | `{symbol, price}` |
| GET | `/api/regime/{symbol}?timeframe=1h` | – | `RegimeAnalysis` |
| GET | `/api/auto/selection?symbol=&style=BALANCED&timeframe=1h` | – | `StrategySelection` (preview of map(regime, style)) |
| GET | `/api/auto/presets-table` | – | `PresetsTable` *(app)*: `{source, entries: [{regime, style, preset \| null, size_multiplier, reason}]}` — all 6 × 3 combinations with every preset enabled, from the wired engine's `select_strategy`. `source` = `"engine:<Class>"` (Bot 2's engine) or `"fallback:<Class>"` (app stand-in until the engine lands). `preset: null` = stay out / pause |

## Manual strategies (Manual Builder)
| Method | Path | Body | Response |
|---|---|---|---|
| GET | `/api/strategies` | – | `ManualStrategy[]` |
| POST | `/api/strategies` | `ManualStrategy` | `ManualStrategy` (saved; same id overwrites) |
| GET | `/api/strategies/{id}` | – | `ManualStrategy` |
| PUT | `/api/strategies/{id}` | `ManualStrategy` | `ManualStrategy` (`version` += 1) |
| DELETE | `/api/strategies/{id}` | – | `{deleted: id}` |
| POST | `/api/strategies/validate` | any JSON | `ManualStrategy` (normalised) or 422 |
| POST | `/api/backtest` | `BacktestRequest`: `{strategy: ManualStrategy, initial_capital?: 10000, limit?: 500}` | `BacktestResult` on the last `limit` bars of full history (same as `candles?history=true&limit=…`); identical results regardless of the loop's position |

## Agent activation & loop
| Method | Path | Body | Response |
|---|---|---|---|
| POST | `/api/manual/activate` | `{strategy_id}` or `{strategy: ManualStrategy}` | `StatusSnapshot` |
| POST | `/api/auto/activate` | `AutoConfig` | `StatusSnapshot` |
| POST | `/api/agent/deactivate` | – | `StatusSnapshot` |
| GET | `/api/loop` | – | `LoopState`: `{running, interval_seconds, ticks, agent_mode, active_symbol, active_strategy, replay_finished}` |
| POST | `/api/loop/start` | `{interval_seconds?: 5}` | `LoopState` |
| POST | `/api/loop/stop` | – | `LoopState` |
| POST | `/api/loop/tick` | – | `Decision[]` (runs exactly one cycle now: exits first, then entry) |

Activation sets the risk confirmation mode from `activation` (`CONFIRMATION` → on, `AUTO_EXECUTE` → off). Full autonomy is the
default in every mode: a request body without an explicit `activation` (AutoConfig, or an inline ManualStrategy) is treated as
`AUTO_EXECUTE`, and a fresh start (no saved state) has `confirmation_mode = false`.
Only one agent (one manual strategy **or** one auto config) is active at a time. Activating an agent (or
deactivating) rejects any still-pending confirmations of the previous one: they become `STAY_OUT` with
`execution.status = REJECTED` and `reason = "Not executed: agent switched"` / `"Not executed: agent deactivated"`.

## Decisions, confirmations, positions
| Method | Path | Body | Response |
|---|---|---|---|
| GET | `/api/status` | – | `StatusSnapshot` |
| GET | `/api/portfolio` | – | `Portfolio` (marked to market); each position + `pending`, `client_order_id`, `held_exit` (see LIVE) |
| GET | `/api/decisions?limit=50&symbol=` | – | `Decision[]` newest first |
| GET | `/api/decisions/{id}` | – | `Decision` |
| GET | `/api/confirmations` | – | `Decision[]` waiting for the user (`execution.status == PENDING_CONFIRMATION`) |
| POST | `/api/confirmations/{decision_id}/approve` | – | `Decision` (fresh ~30 s quote, then executed; `EXPIRED` if the pending decision is older than `PENDING_TTL_SECONDS`, default 300) |
| POST | `/api/confirmations/{decision_id}/reject` | `{reason?}` | `Decision` (action `STAY_OUT`, execution `REJECTED`) |
| POST | `/api/positions/by-id/{position_id}/close` | – | `Decision` — closes ONE lot (`Position.id`) at the last price; `signal.exit_reason = MANUAL`, `signal.position_id` = that id. 404 if no open position has that id |
| POST | `/api/positions/{symbol}/close` | – | `Decision` — closes ALL lots of `symbol` (manual override, `ExitReason.MANUAL`) |

## Control (risk layer)
| Method | Path | Body | Response |
|---|---|---|---|
| GET | `/api/control` | – | `ControlState` |
| POST | `/api/control/pause` | `{reason?}` | `ControlState` |
| POST | `/api/control/resume` | – | `ControlState` (clears `paused` only; does **not** clear an emergency stop) |
| POST | `/api/control/emergency-stop` | `{reason?, flatten?: false}` | `ControlState` (sets `emergency_stopped`; rejects pending confirmations; blocks every BUY. `flatten=true` closes all positions immediately at the last price; otherwise the risk layer closes them on the next bar at the bar close with `exit_reason = EMERGENCY_STOP`) |
| POST | `/api/control/reset-emergency-stop` | – | `ControlState` (clears `emergency_stopped`; leaves `paused` unchanged) |
| POST | `/api/control/confirmation-mode` | `{enabled: bool}` | `ControlState` |

Protective exits (SL / TP / trailing / emergency flatten) execute without confirmation; entries obey confirmation mode.
While `emergency_stopped` every cycle is STAY_OUT (protective exits still run) until `reset-emergency-stop`.

Exit decisions: `Decision.signal.exit_reason` (`ExitReason`) says why a position was closed, `Decision.signal.position_id`
which `Position.id` it closed, and `Decision.signal.price` the intended fill (SL/TP level or gap open). Every
`Position` in `Portfolio.positions` has a stable `id`; there can be several lots per symbol.

## Monitoring
| Method | Path | Body | Response |
|---|---|---|---|
| GET | `/api/alerts?limit=50` | – | `Alert[]`: `{id, level: INFO\|WARNING\|CRITICAL, code, message, symbol?, timestamp}` |
| GET | `/api/performance` | – | `PerformanceSummary`: `{starting_capital, equity, realized_pnl, unrealized_pnl, total_return_pct, max_drawdown_pct, win_rate_pct, num_trades, wins, losses, consecutive_losses, equity_curve: EquityPoint[]}` (equity marked to market every tick → drawdown not understated) |

Alert codes: `EMERGENCY_STOP`, `PAUSED`, `CONSECUTIVE_LOSSES` (≥3), `DAILY_LOSS` , `QUOTE_EXPIRED`, `EXECUTION_FAILED`,
`RISK_BLOCKED`, `LIVE_MODE`, `COMPONENT_FALLBACK`, `REPLAY_FINISHED` (SIM replay hit the last bar), `TICK_FAILED`.

## LLM (Bot 5, never trades)
| Method | Path | Body | Response |
|---|---|---|---|
| POST | `/api/llm/parse` | `{text, default_symbol?}` | `{kind: "MANUAL", strategy: ManualStrategy}` or `{kind: "AUTO", config: AutoConfig}`, plus `notes: string[]` (Bot 5 parser notes, e.g. the default entry rule it filled in; `[]` if none) |
| POST | `/api/llm/ask` | `{question}` | `{answer}` |
| GET | `/api/llm/summary` | – | `{summary}` |

`summary` / `ask` give the explainer the current `StatusSnapshot` plus `PerformanceSummary` and the latest 20 `Alert`s
(passed as `performance=` / `alerts=` when the installed explainer accepts them; older signatures still work).

## WebSocket `/ws`
Server → client: one contract `Event` per message: `{type: EventType, payload, timestamp}`.

| `type` | `payload` | When |
|---|---|---|
| `STATUS` | `StatusSnapshot` | on connect, after each tick, after activation / control changes |
| `DECISION` | `Decision` | every decision (BUY/SELL/STAY_OUT, incl. pending confirmations and rejections) |
| `EXECUTION` | `ExecutionResult` + `decision_id` | every order result (FILLED / EXPIRED / REJECTED / FAILED) |
| `CONTROL` | `ControlState` | pause / resume / emergency stop / confirmation mode |
| `PRICE` | `{symbol, price, candle: Candle}` | each tick, for the active symbol |
| `ALERT` | `Alert` | see alert codes |

Client → server (optional): `{"type": "ping"}` → server replies with a `STATUS` event.

## Modes
`TRADING_MODE=SIM` (default; offline mocks, the loop replays seeded candles bar by bar, `SIM_SEED`, default 42),
`PAPER` (real market data, simulated fills), `LIVE` (refused at startup unless `LIVE_TRADING_ENABLED=true` **and**
`BINANCE_LIVE_ENABLED=true` **and** `BINANCE_API_KEY` / `BINANCE_API_SECRET` are set; the startup error names each missing
item, e.g. `missing: BINANCE_LIVE_ENABLED=true, BINANCE_API_SECRET`). PAPER/LIVE quotes for xStock tokens (`...x`) are RFQ
and need `BINANCE_WALLET_ADDRESS`; without it Bot 4's client refuses the quote (40001) and the decision shows
`BUY not filled (FAILED: QuoteUnavailable ...)`. The execution path is whatever the exchange quotes per token/route
(SIM: Ondo + xStock → RFQ, bStock → SWAP); the UI should not assume a path per token. The mode is fixed per process and reported in
`/api/system`, `ControlState.trading_mode` and every `Decision.mode`.

## Reason texts (exactly one prefix)
- Exit decisions: `"<EXIT_REASON>: <detail>"`, e.g. `"RSI_EXIT: RSI(14)=83.8 > 70"`, `"STOP_LOSS: low 94 reached stop loss 95 ..."`, `"MANUAL: close requested by user"`.
- Blocked entries: `"BUY not executed: Blocked (<check>): <detail>"` (one "Blocked" per failed check).
- Control / alerts: `ControlState.reason` is the raw reason (`"lunch"`, `"Paused by user"`, `"Emergency stop by user"`);
  alerts read `"Paused: lunch"`, `"Paused by user"`, `"Emergency stop: test"`, `"Emergency stop by user"`.
  Decisions during an emergency stop: `"Emergency stop: <reason>; staying out"`.
- User rejection: `"Rejected by user: <reason>"` (or `"Rejected by user"`).

## Execution path
The loop executes on the path the exchange quoted: `Quote.path == RFQ` → `execute_rfq`, otherwise `execute_swap`
(the exchange rejects an order sent to the wrong path). `ExecutionResult.path` shows which one was used.

## LIVE (24/7 autonomous agent, real funds on BSC) — app-side shapes (v0.7)
These fields/endpoints are app-only (`backend/app/schemas.py`) until the contracts carry them. SIM/PAPER payloads
are unchanged except where noted (positions always carry `pending`).

| Method | Path | Response |
|---|---|---|
| GET | `/api/live/status` | `{live_trading_enabled, binance_live_enabled, risk_allow_live, wallet_connected, can_trade, blocked_reasons: string[], cooldowns: [{symbol, code, until, reason}], chain_pending: [{order_id, client_order_id, symbol, notional, tx_hash, since, held_exit}]}` |
| GET | `/api/system` | `SystemInfo` + `live` (= `/api/live/status`) + `agent_identity` (see below) |
| GET | `/api/wallet` | `{provider: 'sim'\|'paper'\|'baw'\|'web3_api', address, chain: 'BSC', chain_id: 56, connected, balances: [{symbol, amount, usd_value}], bnb_gas_reserve, limits, updated_at}` |
| GET | `/api/loop` | `LoopState` + `heartbeat: {last_tick_at, seconds_since_tick, restarts, last_error}` |

`limits` = Bot 3's `risk.live_limits(portfolio, LiveContext)` keys as-is (`limit_source`, `wallet_limits`,
`risk_limits`, `exit_quota_shortfall_usdt`, `min_wallet_usdt`, ...; every key passes through, new ones included) plus always: `daily_limit_usdt`, `daily_used_usdt` (settled fills +
in-flight buys), `daily_remaining_usdt`, `per_order_cap_usdt`, `allowed_tokens`, `tradable_tokens`, `warnings` (list,
default `[]`, passed through unchanged; a new code raises alert `LIVE_LIMIT_<CODE>`, e.g. `exit_quota_shortfall`,
`wallet_limits_stale`).
- Token-aware: `live_limits()` gets a `LiveContext` with the **active (or `DEFAULT_AGENT_SYMBOL`) token's status**, so
`min_order_usdt` / `min_wallet_usdt` / `min_wallet_usdt_note` follow that token (NVDAon: 20 USDT minimum). `limits_symbol`
names it. `per_token: {SYMBOL: {min_order_usdt, min_wallet_usdt, min_wallet_usdt_note}}` covers every tradable token
(only `min_order_usdt` when risk has no `min_wallet_info`).
- Each tick (and each `/api/wallet` read) passes the same limits to the explainer's `set_live_limits(limits)` (Bot 5) for
small-wallet / gas hints.
- `allowed_tokens` is the **wallet/risk-side** restriction only, passed through as-is: a list of symbols, the string
  `'ALL'` (no restriction) or `'LIMITED'` (the wallet restricts tokens but did not list them), or `null` (unknown).
  It is never filled from the exchange token list.
- `tradable_tokens` = tokens the agent can be activated on: the exchange token list (minus xStock `…x` in LIVE), or
  `LIVE_ALLOWED_TOKENS` when set.
- Wallet limits (`baw wallet settings`) are re-read every 10 min and after every execution; the daily quota resets
  at 00:00 UTC = 05:30 IST.

`/api/system.agent_identity` = Bot 5's `backend.agent_studio.get_agent_identity()` passed through with every key
(`erc8004_id`, `registry`, `studio_version`, `network`, `explorer_url`, ...); `null` until registered or when the module
is absent.

### Executions (LIVE only; keys omitted when unset, so SIM/PAPER stay contract-shaped)
`ExecutionResult` + `explorer_url` (`https://bscscan.com/tx/<hash>`), `route`, `real_funds: true`, `quote_expires_at`,
`price_impact_pct` (percent units; omitted when unknown, never 0 for unknown), `gas`, `wallet_order_id`. Values from Bot 4's
`executor.extras[client_order_id]` win over app-computed ones. The same execution (result + extras) is what the
explainer receives, so LIVE explanations carry the BscScan link.

### In-flight buys (`PENDING_CONFIRMATION` = submitted, waiting on chain — not a failure)
- The BUY decision reads `"BUY submitted, waiting on chain ..."`. `risk.record_execution(res, quote_notional=<quote amount>)` is called once
  at submission and again with the final result for the same `client_order_id` after reconcile. Risk's slippage cap
  (`live_limits().max_slippage_pct`) is passed per order as `execute_swap/execute_rfq(..., max_slippage_pct=)`.
- Until `executor.reconcile()` (called every tick) settles it, the buy is a **synthetic open position** at its quote
  amount in the portfolio given to the engine and risk (no second BUY; counts toward exposure / max positions / cash).
  Orders found open in Bot 4's intent journal after a restart are added the same way.
- `/api/portfolio`, `STATUS.portfolio` (HTTP + WS): every position has `pending: bool`, `client_order_id`
  (in-flight only, else `null`) and `held_exit: {code: 'pending_exit', reason, source} | null`.
- Settled FILLED → replaced by the real lot (`"confirmed on chain; ..."`); FAILED → dropped (`"BUY failed on chain (...)"`).
- A SELL aimed at a pending position (engine signal, `risk.check_exits` SL/TP/trailing, emergency stop) is never sent:
  decision `STAY_OUT`, reason `"exit held: buy still settling on-chain [pending_exit] (<EXIT_REASON> via <source>)"`,
  `signal.conditions_failed` includes `pending_exit`; `source` ∈ `engine`, `risk.check_exits`, `emergency_stop`.
  When the fill confirms, exits are re-run on the real position in the same tick (an emergency stop sells at once);
  if the buy fails the held exit is dropped (`"...; held exit dropped (<reason>)"`).
- Bot 4 refusal `order_in_flight` (an earlier order is unresolved): `STAY_OUT`, reason
  `"BUY not executed: order_in_flight (...)"`, `signal.conditions_failed` includes `order_in_flight`; no alert.

### Exchange states (normal decisions, never exceptions)
- No quote (market closed, no liquidity, xStock disabled, below minimum — any mode): `"BUY not executed: no quote (<detail>)"`.
- Codes 40367 / 40369 / 40374 additionally start a 20-minute per-token cooldown: `"... cooling down until HH:MM UTC"`
  and later `"BUY not executed: <symbol> cooling down until HH:MM UTC after exchange error <code>"`; listed in `cooldowns`.
- `WALLET_SESSION_EXPIRED` / executor reasons `session_expired`, `wallet_not_ready`: the agent is paused and a CRITICAL
  alert `WALLET_SESSION_EXPIRED` is raised (re-scan the QR with `baw`, then resume).
- Bot 2 live engine `STAY_OUT` codes (`live_below_min_order`, `live_illiquid_family`, `live_token_paused`,
  `live_price_impact`, `insufficient_data`, stale data) are logged no-trade decisions. Before each evaluation the app calls
  `engine.set_market_context(symbol, {status, reference_price_only, min_order_usdt, priceImpactPercent, equity})`
  (`status` as a plain string such as `OFFHOURS`; `min_order_usdt` = `get_token_status(symbol).min_order_usdt`, the same
  floor risk applies, `null` if unavailable; `priceImpactPercent` / `equity` omitted when unknown) and sets
  `live_config.max_order_usdt` from `risk.live_limits()['per_order_cap_usdt']`. Engine and risk agree on price impact:
  > 1.0 % blocks, exactly 1.0 % is allowed.

Alert codes added: `WALLET_SESSION_EXPIRED`, `WALLET_SETTINGS`, `LIVE_LIMIT_<CODE>`, `CHAIN_PENDING` (unconfirmed after 30 min),
`LOOP_RESTARTED`, `LOOP_STALLED` (no tick for 3 intervals), `STATE_RESTORED`, `STATE_RESTORE`, `AUTOSTART`.

## Wallet sign-in from the dashboard (v0.8, localhost only)
All three endpoints answer **403** ("Wallet sign-in only works on the PC running the Regime backend ...") unless the
request comes from `127.0.0.1` / `::1` / `localhost` (an `X-Forwarded-For` with a non-local address is also refused).
The Vite dev proxy connects from localhost, so the dashboard on `http://localhost:5173` works.

| Method | Path | Body / query | Returns |
|---|---|---|---|
| POST | `/api/wallet/connect` | - | `{session_id, qr_url, pairing_code, expires_at}` (`expires_at` ISO UTC, ~5 min). 502 if `baw auth signin` fails |
| GET | `/api/wallet/connect` | `?session_id=` | `{status: pending\|connected\|expired\|failed, address, wallet_changed, error}`; unknown id = `failed` |
| POST | `/api/wallet/reconcile` | - | `{address, was_blocked, kept[], dropped[], wallet_blocked: false}`; 409 while orders settle or balances are unavailable |

- One process-wide `backend.exchange.wallet_connect.WalletConnect` (Bot 4): `start()` runs `baw auth signin` and a
  background `baw auth verify`; starting a new sign-in cancels the previous one.
- **LIVE** uses the real `baw` (BAW_CMD / PATH). **SIM/PAPER** use the bundled fake `backend/app/dev_baw.py`
  (connects after `FAKE_BAW_VERIFY_S`, default 6 s; `FAKE_BAW_OUTCOME=ok|expired|failed`; `FAKE_BAW_ADDRESS`), so the
  dashboard flow can be demoed without Binance.
- `wallet_changed` compares the signed-in BSC address with the saved one: `wallet_address` in
  `agent_state.<mode>.json`, else `BINANCE_WALLET_ADDRESS`. The first successful sign-in (or a reconcile) saves it.
- On the first `connected` poll the backend drops its cached wallet reads (balances, `baw wallet settings` limits,
  token statuses) and re-reads them, so the next `/api/wallet` has a newer `updated_at` and fresh `limits`.
- `wallet_changed: true` blocks every order (BUYs **and** exits, since the saved positions may not be in the new
  wallet): `/api/live/status` (and `/api/system.live`) gets `can_trade: false`, `"different wallet connected"` in
  `blocked_reasons` and `wallet_block: {address, expected, since}`; each tick is a STAY_OUT "Blocked: different wallet
  connected ...". The block is saved in the state file and survives restarts.
- `POST /api/wallet/reconcile` checks the saved open positions against the connected wallet's balances (oldest
  first). Positions the wallet doesn't fully hold are dropped from the book (no trade; listed in `dropped`), the
  wallet becomes the saved wallet and the block clears. In SIM the wallet is the book, so nothing is dropped.

## Changelog
- v0.8: dashboard wallet sign-in (`POST/GET /api/wallet/connect`, `POST /api/wallet/reconcile`, localhost only), `wallet_block` in `/api/live/status`, "different wallet connected" LIVE block, fake baw for SIM/PAPER; app title "Regime".
- v0.7: LIVE section (wallet, live status, cooldowns, in-flight buys with synthetic `pending` positions and `pending_exit` held exits, `order_in_flight`, executor extras on executions, `agent_identity` pass-through, `allowed_tokens` 'ALL'/'LIMITED' pass-through + `tradable_tokens`); `/api/portfolio` positions gain `pending`, `client_order_id`, `held_exit`; an unavailable quote is a STAY_OUT decision in every mode; `/api/tokens` adds `token_status`; activation defaults to `AUTO_EXECUTE` unless the request sets `activation`; `record_execution(res, quote_notional=)` at submission + after reconcile; slippage cap passed per order.
- v0.6: LIVE needs both `LIVE_TRADING_ENABLED=true` and `BINANCE_LIVE_ENABLED=true` (+ keys); no per-token path assumptions.
- v0.5: `GET /api/candles/{symbol}?history=true`; `/api/backtest` always uses the cached full history (no longer drifts with the replay).
- v0.4: SIM market data never passes the loop's replay bar; agent switch rejects pending confirmations; single-prefix reason texts.
- v0.3: `GET /api/auto/presets-table`, `POST /api/positions/by-id/{position_id}/close`; emergency-stop exits at bar
  close; RFQ/SWAP dispatch on `Quote.path`; LLM summary/ask get performance + alerts; protective-exit edge rules (any stop beats TP, highest stop wins, strict trailing).
- v0.2 (contracts 0.2.0): app types moved into the contracts; `POST /api/control/reset-emergency-stop` added and
  `resume` no longer clears an emergency stop; exits expose `Signal.exit_reason` / `Signal.position_id`; `Position.id`.
