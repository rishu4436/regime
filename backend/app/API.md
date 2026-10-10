# Backend API (Bot 6) — REST + WebSocket

Base URL `http://localhost:8000`. The Vite dev server proxies `/api`, `/health` and `/ws`.
All bodies are JSON. Every model name below is a contract model from `backend/contracts`
(TypeScript: `frontend/src/contracts.ts`). Timestamps are ISO-8601 UTC. Errors: `{"detail": "..."}` with
4xx status (`404` unknown id/symbol, `409` invalid state such as "no active agent", `422` validation error).

Types that are **not (yet) in the contracts** are marked *(app)* and defined in `backend/app/schemas.py`;
they are proposed as contract additions (see bottom).

## System
| Method | Path | Body | Response |
|---|---|---|---|
| GET | `/health` | – | `{status, contracts_version, trading_mode}` |
| GET | `/api/system` | – | `SystemInfo` *(app)*: `{trading_mode, live_enabled, contracts_version, sim_seed, components: {engine, risk, exchange, explainer}}` (`components.*` = class name, or `"fallback:<name>"` while a bot's module is still a stub) |

## Market data
| Method | Path | Body | Response |
|---|---|---|---|
| GET | `/api/tokens?q=` | – | `TokenInfo[]` |
| GET | `/api/candles/{symbol}?timeframe=1h&limit=200` | – | `Candle[]` (oldest first) |
| GET | `/api/price/{symbol}` | – | `{symbol, price}` |
| GET | `/api/regime/{symbol}?timeframe=1h` | – | `RegimeAnalysis` |
| GET | `/api/auto/selection?symbol=&style=BALANCED&timeframe=1h` | – | `StrategySelection` (preview of map(regime, style)) |

## Manual strategies (Manual Builder)
| Method | Path | Body | Response |
|---|---|---|---|
| GET | `/api/strategies` | – | `ManualStrategy[]` |
| POST | `/api/strategies` | `ManualStrategy` | `ManualStrategy` (saved; same id overwrites) |
| GET | `/api/strategies/{id}` | – | `ManualStrategy` |
| PUT | `/api/strategies/{id}` | `ManualStrategy` | `ManualStrategy` (`version` += 1) |
| DELETE | `/api/strategies/{id}` | – | `{deleted: id}` |
| POST | `/api/strategies/validate` | any JSON | `ManualStrategy` (normalised) or 422 |
| POST | `/api/backtest` | `BacktestRequest` *(app)*: `{strategy: ManualStrategy, initial_capital?: 10000, limit?: 500}` | `BacktestResult` |

## Agent activation & loop
| Method | Path | Body | Response |
|---|---|---|---|
| POST | `/api/manual/activate` | `{strategy_id}` or `{strategy: ManualStrategy}` | `StatusSnapshot` |
| POST | `/api/auto/activate` | `AutoConfig` | `StatusSnapshot` |
| POST | `/api/agent/deactivate` | – | `StatusSnapshot` |
| GET | `/api/loop` | – | `LoopState` *(app)*: `{running, interval_seconds, ticks, agent_mode, active_symbol, active_strategy, replay_finished}` |
| POST | `/api/loop/start` | `{interval_seconds?: 5}` | `LoopState` |
| POST | `/api/loop/stop` | – | `LoopState` |
| POST | `/api/loop/tick` | – | `Decision[]` (runs exactly one cycle now: exits first, then entry) |

Activation sets the risk confirmation mode from `activation` (`CONFIRMATION` → on, `AUTO_EXECUTE` → off).
Only one agent (one manual strategy **or** one auto config) is active at a time.

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
| POST | `/api/positions/{symbol}/close` | – | `Decision` (manual override, `ExitReason.MANUAL`) |

## Control (risk layer)
| Method | Path | Body | Response |
|---|---|---|---|
| GET | `/api/control` | – | `ControlState` |
| POST | `/api/control/pause` | `{reason?}` | `ControlState` |
| POST | `/api/control/resume` | – | `ControlState` (also clears an emergency stop) |
| POST | `/api/control/emergency-stop` | `{reason?, flatten?: false}` | `ControlState` (`flatten=true` also closes all positions) |
| POST | `/api/control/confirmation-mode` | `{enabled: bool}` | `ControlState` |

Protective exits (SL / TP / trailing / emergency flatten) execute without confirmation; entries obey confirmation mode.

## Monitoring
| Method | Path | Body | Response |
|---|---|---|---|
| GET | `/api/alerts?limit=50` | – | `Alert[]` *(app)*: `{id, level: INFO\|WARNING\|CRITICAL, code, message, symbol?, timestamp}` |
| GET | `/api/performance` | – | `PerformanceSummary` *(app)*: `{starting_capital, equity, realized_pnl, unrealized_pnl, total_return_pct, max_drawdown_pct, win_rate_pct, num_trades, wins, losses, consecutive_losses, equity_curve: EquityPoint[]}` (equity marked to market every tick → drawdown not understated) |

Alert codes: `EMERGENCY_STOP`, `PAUSED`, `CONSECUTIVE_LOSSES` (≥3), `DAILY_LOSS` , `QUOTE_EXPIRED`, `EXECUTION_FAILED`,
`RISK_BLOCKED`, `LIVE_MODE`, `COMPONENT_FALLBACK`, `REPLAY_FINISHED` (SIM replay hit the last bar), `TICK_FAILED`.

## LLM (Bot 5, never trades)
| Method | Path | Body | Response |
|---|---|---|---|
| POST | `/api/llm/parse` | `{text, default_symbol?}` | `{kind: "MANUAL", strategy: ManualStrategy}` or `{kind: "AUTO", config: AutoConfig}` |
| POST | `/api/llm/ask` | `{question}` | `{answer}` |
| GET | `/api/llm/summary` | – | `{summary}` |

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

## Proposed contract additions (pending coordinator)
`Alert`, `PerformanceSummary`, `SystemInfo`, `LoopState`, `BacktestRequest` (shapes above);
`Signal.exit_reason: ExitReason | None`; `RiskManager.check_exits(..., bars: dict[str, Candle] | None = None)`.
