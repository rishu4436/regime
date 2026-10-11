# Integration and monitoring

*Part of the Developer Experience Report for team `teamwork` (BNB Hack: Tokenized Stocks). Written 11 Oct 2026 by
Bot 6 (Integration & QA). Every claim points at code on `main` after PR #11 (`backend/app`, `backend/monitoring`,
`tests/e2e`). Nothing in this section has been run with real funds; all numbers come from SIM runs with no keys and
no LIVE switches set.*

## 1. Integration approach

**Contracts first.** Six bots built six modules in parallel, so the only shared surface was `backend/contracts`
(Pydantic models plus `StrategyEngine`, `RiskManager`, `ExchangeClient` and `Explainer` Protocols). The frontend types
in `frontend/src/contracts.ts` are generated from it, and CI runs `python scripts/gen_contracts.py --check` so the two
can never drift. Contract changes went through the coordinator, never through a bot's own module.

**Fallbacks so the loop always runs.** `backend/app/fallbacks.py` has simple implementations of every Protocol, used
only while a bot's module still raises `NotImplementedError`. `backend/app/wiring.py` picks up the real classes
automatically once they ship. This meant the API, the dashboard and the e2e tests worked from day one, and each bot's
work could be dropped in without touching the app.

**Combined merges.** Each bot pushed its own branch (`bot1/*` … `bot6/*`). I merged them, in order, into one
integration branch (`integ-v7` … `integ-v9`, then `integ/live`) and ran the full backend suite and frontend build after
**every** merge, not just at the end. The branch only went to `main` as one PR (#11) after the combined build was green
locally and on GitHub CI. That PR replaced the per-bot PRs #8 and #9.

## 2. Testing

All e2e tests live in `tests/e2e` and drive the real FastAPI app through `TestClient` in SIM.

- **Manual and Auto flows** (`test_manual_flow.py`, `test_auto_flow.py`) go from strategy activation through loop
  ticks to decisions, positions and alerts.
- **Exit parity** (`test_exit_parity.py`): the backtester (engine) and the live exit check (risk) must pick the **same
  bar, reason and fill price** on seeded candles and on hand-built fixtures. Fixtures include:
  - stop-loss and take-profit hit in the same bar: **stop-loss wins** at the stop level;
  - gap down through the stop: fills at the **open**, not at the stop;
  - gap up through take-profit: fills at the open;
  - gap up through take-profit then down through the stop in one bar: the stop still wins;
  - trailing stop that uses the previous bar's level, a trailing gap down that fills at the open, and max-loss vs
    stop-loss ordering.
- **Price-impact parity** (`test_live.py`): the engine (live-safe mode) and risk must both block a LIVE buy when price
  impact is above **1.0%** and both allow exactly 1.0%. The test covers 0.5, 0.99, 1.0, 1.01, 1.5 and 2.5%.
- **In-flight orders** (`test_live.py`): a LIVE buy whose outcome is unknown is recorded as a pending position. That
  blocks a second buy, counts toward exposure and position limits, is swapped for the real position when the fill
  confirms, and is dropped when it fails. A stop-loss on a pending position is **held** rather than sold, and an
  emergency stop sells in the tick where the fill confirms.
- **Mode separation** (`test_modes.py`), replay safety (`test_replay_view.py`), and QA regressions
  (`test_qa_regressions.py`).

**Result on `main` (fresh clone, SIM, no keys): 894 passed, 2 skipped.** The two skips are expected on a clean
machine: the `bnbagent` SDK is not installed, and the PAPER-mode test needs Binance keys.

## 3. 24/7 operation

- **Autostart.** `LOOP_AUTOSTART=true` resumes the saved agent on boot. If none is saved, it starts Auto on
  `DEFAULT_AGENT_SYMBOL` (NVDAB in PAPER/LIVE).
- **Watchdog.** If a loop cycle fails, the loop retries with backoff and raises a `TICK_FAILED` alert. If the loop task
  itself crashes, a supervisor restarts it with exponential backoff from **1 s to 60 s** (`LOOP_RESTARTED`, critical).
  A monitor also restarts a dead task and raises a stall alert when no tick happens for `stall_intervals` (default 3)
  loop intervals.
- **Heartbeat.** `/api/status` reports `started_at`, uptime, `last_tick_at`, seconds since the last tick, restart count
  and last error. Every cycle counts as a tick, even when there is no new bar.
- **Persisted state.** `agent_state.<mode>.json` (in `STATE_DIR` or `LOG_DIR`) is written atomically. It holds the
  active agent, the control state, the LIVE daily-notional ledger and, outside SIM, open lots. **An emergency stop
  survives a restart** and must be reset by hand. There is one file per mode, so a SIM session can never resume into
  LIVE. No secrets are stored.
- **Supervised run scripts.** `backend/app/deploy/run.sh --supervised` (and `run.ps1` on Windows) restarts the backend
  whenever it exits, with a 2–60 s backoff, and turns on autostart. A config error (LIVE refused) is checked before the
  first start and exits instead of looping.

## 4. LIVE gating

LIVE is off unless **all three** switches are exactly `true`: `LIVE_TRADING_ENABLED` (app), `BINANCE_LIVE_ENABLED`
(exchange) and `RISK_ALLOW_LIVE` (risk). Keys must also be set. Otherwise the app refuses to start in LIVE and names
what is missing (`settings.py`). Risk also rejects every LIVE buy unless its own `allow_live` is set from those same
switches (see [risk.md](risk.md)). The loop rejects any order whose exchange mode differs from the app's mode. The
steps are in `backend/app/GO_LIVE.md`.

## 5. Pain points and suggested fixes

1. **The reference simulator isn't deterministic.** `reference/full_strategy_builder_simulation.html` generates candles
   with `Math.random()`, so its results couldn't be reproduced or used as test oracles. *Fix:* ship a seeded generator
   or fixed sample candles with the reference. We seeded our mock data (`test_mock_data_is_seeded`).
2. **Reason prefixes got duplicated.** Engine, risk and the app could each add a prefix such as "Blocked" or
   "Emergency stop" to the same reason, so it could repeat in the UI and in explanations. *Fix:* one shared helper (`one_prefix` in `backend/monitoring`)
   and a shared reason vocabulary in the contracts from the start.
3. **The replay index leaked future bars.** In SIM replay, `/api/candles`, prices, tokens and the regime preview could
   show bars after the loop's current bar. *Fix:* every read path stops at the replay bar, through the client's
   `set_replay_index` or by trimming in the app (`test_replay_view.py`).
4. **Engine and risk couldn't see pending swaps.** An on-chain swap with an unknown outcome wasn't a position, so the
   engine could buy again and risk under-counted exposure. *Fix:* synthetic pending positions from the executor's
   intent journal, counted by risk, with exits held until the fill confirms.
5. **The contracts had gaps for live fields.** Price impact, token status, wallet limits, cooldowns and chain-pending
   counts weren't in the original contracts, so bots passed them through as extras and needed several contract
   bumps (API.md v0.7). *Fix:* model live-only fields in the contracts on day one, even if they're optional.
6. **No Binance testnet for e2e.** The Web3 API and Agentic Wallet have no testnet, so LIVE paths are tested only
   against fake exchanges and fake executors in SIM. Real fills haven't been verified. *Fix (for Binance):* a sandbox
   or testnet for Web3 swaps and the Agentic Wallet.
