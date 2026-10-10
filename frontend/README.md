# `frontend` — Bot 1 — UI & Manual Builder

**Owner:** Bot 1 — UI & Manual Builder. This bot edits only this directory.

## Scope (verbatim from the spec PDF)
**Goal:** Build the complete user interface for both Manual and Auto modes.

- Full Manual Strategy Builder matching the simulator (all indicators, AND/OR, exits, risk, filters)
- Agent selection + trader style selector for Auto Mode
- Dashboard showing current regime, active strategy, open positions, PnL, last decision reason
- Simulation results view
- Confirmation, pause, and override controls
- Reference the existing full_strategy_builder_simulation.html for UI and interaction patterns

## Interface it must implement
Consumes the REST/WebSocket API served by `backend/app` (Bot 6). All request/response types come from
`src/contracts.ts`, which is **generated** from `backend/contracts` — never edit it by hand
(`python scripts/gen_contracts.py` regenerates it). Build `ManualStrategy` JSON exactly as typed there; defaults
mirror `reference/full_strategy_builder_simulation.html`.

## Definition of done
- React + Vite + TypeScript app in this directory; `npm run build` passes with zero TS errors.
- Builder covers every `ManualStrategy` field (see `reference/README.md` for the simulator mapping), Auto Mode
  config (`AutoConfig`), dashboard (`StatusSnapshot`, `Decision`), simulation results (`BacktestResult`) and controls (`ControlState`).
- Works against the SIM backend (`uvicorn backend.app.main:app`), with a mock-data fallback when the backend is down.
- Dev: `npm run dev` (proxy `/api` and `/ws` to `http://localhost:8000`, configured in `vite.config.ts`).
- No edits outside `/frontend`. Contract changes go through the coordinator.

## Running the UI (Bot 1)

```bash
cd frontend && npm install
npm run dev                                   # http://localhost:5173, in-browser mock API (default)
VITE_API_MODE=http npm run dev                # talk to the backend via the dev proxy (/api, /ws -> :8000)
VITE_API_MODE=http VITE_API_URL=http://localhost:8000 npm run build   # static build against a given backend
```

### Mock vs real API
All network access goes through `src/api/` behind one typed interface (`ApiClient` in `src/api/types.ts`, types from
the generated `src/contracts.ts`):

- `src/api/mock.ts` (**`VITE_API_MODE=mock`, default**): runs entirely in the browser. Seeded mock candles per token
  (NVDAon mild uptrend, TSLAx high volatility, AAPLB sideways), a live bar loop every 3 s with `Decision`s and reasons,
  confirmation flow, pause/resume, emergency stop, manual override, and client-side backtests (`src/lib/strategy.ts`,
  simulator semantics plus the backend's intrabar exit rules: SL/TP vs bar low/high with SL first, trailing stop vs the
  previous bar's level, gap fills at the open, rolling 20-bar volume average), a keyword parser standing in for
  `llm/parse`, and canned `llm/ask` answers. The Auto Mode regime detector and
  (regime, style) → preset map in `src/lib/auto.ts` are **indicative only**; the real ones are Bot 2's.
- `src/api/http.ts` (**`VITE_API_MODE=http`**): Bot 6's FastAPI exactly as documented in `backend/app/API.md`
  (REST under `/api` + `/ws` `Event` stream with auto-reconnect and ping). Base URL from `VITE_API_URL` (empty = same origin).
  4xx errors (`404` unknown id, `409` invalid state, `422` validation, `{"detail": ...}`) become `ApiError`s shown as toasts.
  The mock implements the same methods, payloads, error codes and WS events (STATUS/DECISION/EXECUTION/CONTROL/PRICE/ALERT),
  so swapping is just the env var. Types marked *(app)* in API.md (`BacktestRequest`, `LoopState`, `Alert`,
  `PerformanceSummary`, `SystemInfo`, llm parse result) live in `src/api/types.ts` until they land in the contracts.

Charts use TradingView `lightweight-charts` (candlesticks with trade markers, equity area); font is Inter (bundled via `@fontsource-variable/inter`).
