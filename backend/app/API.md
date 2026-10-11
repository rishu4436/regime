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
| GET | `/api/tokens?q=` | – | `TokenInfo[]` |
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

Activation sets the risk confirmation mode from `activation` (`CONFIRMATION` → on, `AUTO_EXECUTE` → off).
Only one agent (one manual strategy **or** one auto config) is active at a time. Activating an agent (or
deactivating) rejects any still-pending confirmations of the previous one: they become `STAY_OUT` with
`execution.status = REJECTED` and `reason = "Not executed: agent switched"` / `"Not executed: agent deactivated"`.

## Decisions, confirmations, positions
| Method | Path | Body | Response |
|---|---|---|---|
| GET | `/api/status` | – | `StatusSnapshot` |
| GET | `/api/portfolio` | – | `Portfolio` (marked to market) |
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
| POST | `/api/llm/parse` | `{text, default_symbol?}` | `{kind: "MANUAL", strategy: ManualStrategy}` or `{kind: "AUTO", config: AutoConfig}` |
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
`BINANCE_API_KEY` / `BINANCE_API_SECRET` are set). The mode is fixed per process and reported in
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

## Changelog
- v0.5: `GET /api/candles/{symbol}?history=true`; `/api/backtest` always uses the cached full history (no longer drifts with the replay).
- v0.4: SIM market data never passes the loop's replay bar; agent switch rejects pending confirmations; single-prefix reason texts.
- v0.3: `GET /api/auto/presets-table`, `POST /api/positions/by-id/{position_id}/close`; emergency-stop exits at bar
  close; RFQ/SWAP dispatch on `Quote.path`; LLM summary/ask get performance + alerts; protective-exit edge rules (any stop beats TP, highest stop wins, strict trailing).
- v0.2 (contracts 0.2.0): app types moved into the contracts; `POST /api/control/reset-emergency-stop` added and
  `resume` no longer clears an emergency stop; exits expose `Signal.exit_reason` / `Signal.position_id`; `Position.id`.
