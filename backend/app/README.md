# `backend/app` — Bot 6 — Integration, Testing & Monitoring (app)

**Owner:** Bot 6 — Integration, Testing & Monitoring. This bot edits only this directory (plus `tests/e2e` and the other Bot 6 dirs).

## Scope (verbatim from the spec PDF)
**Goal:** Assemble, test, and observe the full system.

- Integrate all modules with clear interfaces
- Full simulation and paper-trading mode
- End-to-end test cases for Manual and Auto flows
- Logging of decisions, risk checks, and executions
- Alerts and performance summaries
- Ensure simulator logic and live engine stay consistent

## Interface it must implement
Bot 6 owns `backend/app` (FastAPI wiring + REST/WebSocket API), `backend/monitoring` and `tests/e2e`.
Wire modules only through the Protocols in `backend/contracts/interfaces.py` (dependency-inject implementations,
fall back to `backend.mocks` until a bot's module is ready). Decision loop per bar:
`RiskManager.check_exits` → `StrategyEngine.evaluate_*` → `RiskManager.check` → (confirm?) → `ExchangeClient.get_quote/execute_*`
→ `Decision` (+ `Explainer.explain_decision`) → log + WebSocket `Event`.

Suggested API (all bodies/responses are contract models): `GET /health`, `GET /api/status` (StatusSnapshot),
`GET /api/tokens?q=`, `GET /api/candles/{symbol}`, `POST /api/strategy/manual` (ManualStrategy),
`POST /api/strategy/auto` (AutoConfig), `POST /api/backtest` (→ BacktestResult), `POST /api/control/{pause|resume|emergency-stop|confirmation}`,
`POST /api/decisions/{id}/confirm`, `POST /api/llm/parse`, `POST /api/llm/ask`, `WS /ws` (Event stream).

## Definition of done
- Implements the interface above exactly (signatures from `backend/contracts/interfaces.py`, data shapes from `backend/contracts/models.py`). `isinstance(obj, <Protocol>)` is True.
- Unit tests live in this directory's `tests/` and pass with `pytest` from the repo root; the whole suite stays green.
- Works fully in **SIM** mode with `backend.mocks` (no network, no API keys).
- No edits outside the owned directory. Contract changes go through the coordinator (see CONTRIBUTING.md).

## Implementation (Bot 6)
- `API.md` - REST + WebSocket shape for the UI (contract models; *(app)* types proposed for the contracts).
- `main.py` - `create_app(settings, components)` + module-level `app`. `loop.py` - the trading loop.
- `wiring.py` - builds engine / risk / exchange / explainer against the Protocols. Until a bot's module is
  implemented, `fallbacks.py` stands in (engine/explainer per method, risk as a whole); `/api/system` shows which.
- `exits.py` - executable statement of the agreed protective-exit rule (parity-tested in `tests/e2e/test_exit_parity.py`).
- `portfolio.py` (cash / lots / PnL, maintains `Position.highest_price` after `check_exits`), `feed.py` (SIM bar replay,
  PAPER/LIVE polling), `events.py` (log + WS fan-out), `store.py` (saved strategies), `settings.py` (env).
- Monitoring in `backend/monitoring` (JSONL event log, alerts, mark-to-market performance).

## Run
- `backend/app/deploy/run.sh` (backend :8000 + Vite :5173) or `run.sh --backend-only`.
- `docker compose -f backend/app/deploy/docker-compose.yml up --build` (UI :8080, API :8000).
  These live here because root files / `scripts/` belong to the coordinator; they can be moved to the root later.
- Env: `TRADING_MODE` (SIM default), `LIVE_TRADING_ENABLED` + `BINANCE_API_KEY/SECRET` (LIVE only), `SIM_SEED`,
  `SIM_REPLAY_BARS`, `STARTING_CAPITAL`, `LOOP_INTERVAL_SECONDS`, `PENDING_TTL_SECONDS`, `LOG_DIR`, `USE_FALLBACKS_ONLY`.
