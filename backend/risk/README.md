# `backend/risk` — Bot 3 — Risk & Control Layer

**Owner:** Bot 3 — Risk & Control Layer. This bot edits only this directory.

## Scope (verbatim from the spec PDF)
**Goal:** Enforce safety on every potential trade.

- Position size and max exposure limits
- Stop-loss / trailing stop enforcement
- Cooldowns and loss limits
- Confirmation mode vs full auto
- Global Pause / Resume / Emergency Stop
- Ability to block any signal from Bot 2

## Interface it must implement
Implement `backend.contracts.RiskManager` in `manager.py` (`DefaultRiskManager`):

```python
check(signal, portfolio, control) -> RiskVerdict        # can always force STAY_OUT (approved=False + reasons)
check_exits(portfolio, prices: dict[str, float], bars: dict[str, Candle] | None = None) -> list[Signal]
    # SL / TP / trailing -> SELL signals with exit_reason, position_id, price = fill (SL wins if both hit; level or gap open)
get_control_state() -> ControlState
pause(reason="") / resume() / emergency_stop(reason="") / reset_emergency_stop() / set_confirmation_mode(enabled) -> ControlState
# resume() clears `paused` only; it does NOT clear an emergency stop
```
Every check must appear in `RiskVerdict.checks` (name, passed, detail) so decisions are explainable.
`requires_confirmation=True` when `ControlState.confirmation_mode` is on. Emergency stop blocks everything until reset.
LIVE mode must be blocked unless explicitly enabled by config.

## Definition of done
- Implements the interface above exactly (signatures from `backend/contracts/interfaces.py`, data shapes from `backend/contracts/models.py`). `isinstance(obj, <Protocol>)` is True.
- Unit tests live in this directory's `tests/` and pass with `pytest` from the repo root; the whole suite stays green.
- Works fully in **SIM** mode with `backend.mocks` (no network, no API keys).
- No edits outside the owned directory. Contract changes go through the coordinator (see CONTRIBUTING.md).

---

## Implementation (Bot 3)

Files: `config.py` (`RiskConfig`, `SymbolRiskRules` — private, not contracts), `exits.py` (pure per-bar exit
evaluation), `manager.py` (`DefaultRiskManager`), `tests/` (pytest). Deterministic, no network, no secrets, thread-safe.

### Usage

```python
from backend.risk import DefaultRiskManager, RiskConfig, is_risk_exit

rm = DefaultRiskManager()                       # ControlState() defaults: SIM, confirmation ON, not paused
rm = DefaultRiskManager(control, config=RiskConfig(max_total_exposure_pct=50), clock=fake_clock)  # tests
rm.apply_manual_strategy(strategy)              # use ManualStrategy.risk / .exit / .activation for strategy.symbol

# per bar / tick (trading loop, Bot 6)
for sell in rm.check_exits(portfolio, prices, bars={sym: last_closed_candle}):   # bars optional (prices-only fallback)
    v = rm.check(sell, portfolio, rm.get_control_state())   # approved; never requires confirmation (protective)
    ...execute: close Position sell.position_id at sell.price (fill), reason sell.exit_reason
# then the APP raises highest_price = max(highest_price or entry, bar.high) for every still-open position
signal = engine.evaluate_manual(...)            # or evaluate_auto
verdict = rm.check(signal, portfolio, rm.get_control_state())
if verdict.approved: notional = verdict.adjusted_notional; wait for user if verdict.requires_confirmation

rm.pause("reason"); rm.resume(); rm.emergency_stop("reason"); rm.reset_emergency_stop()
rm.set_confirmation_mode(False); rm.set_trading_mode(TradingMode.PAPER)   # LIVE refused unless allow_live
rm.exit_levels(position, portfolio)            # -> ExitLevels(stop_loss, take_profit, trailing_stop, max_loss, ...) for the UI
```

### `check(signal, portfolio, control) -> RiskVerdict`
Every verdict has `checks` (one `RiskCheck` per rule, `name` / `passed` / human-readable `detail`) and `reasons`
(approved: one summary line + any size reductions; blocked: `"Blocked (<check>): <detail>"` per failed check).
`adjusted_notional` / `adjusted_size_pct` are 0 when blocked.

**Control state used = most restrictive of the manager's own state and the `control` passed in** (paused OR,
emergency OR, confirmation OR, LIVE if either says LIVE). So pass `rm.get_control_state()`; a stale caller copy can
never un-pause the agent.

| Signal | Checks (in order) |
|---|---|
| `STAY_OUT` | `signal_action` (failed) - never approved |
| `BUY` | `signal_action`, `trading_mode` (LIVE blocked unless `allow_live`), `emergency_stop`, `paused`, `max_open_positions` (`len(positions) < max`, simulator gate), `cooldown` (`bars_since_exit[symbol] > cooldown_bars`, missing = no recent exit), `daily_loss_limit` (`daily_pnl > -X% * starting_capital`, default 5%), `max_trades_per_day` (off by default), then sizing: `position_size`, `cash_available`, `max_token_exposure` (default 30% of equity), `max_total_exposure` (default 60%), `max_loss_per_trade` (OFF by default), `min_order_notional` (default 10 USDT), and `confirmation` (if approved) |
| `SELL` | `signal_action`, `trading_mode`, `emergency_stop` (always passes — closing is allowed), `paused` (passes unless `allow_exits_when_paused=False`), `open_position` (must hold the symbol), `sell_size` (full position unless `proposed_notional` is smaller), `confirmation` |

**Sizing (BUY):** desired size = `signal.proposed_notional` if > 0, else `signal.proposed_size_pct` if > 0, else
`position_size_pct` (default 15%, simulator) — always capped at `position_size_pct` of **equity**. Then reduced (never
increased) by cash, per-token exposure, total exposure (exposure valued at `current_price`, else entry price) and, if
enabled, max-loss-per-trade (`notional <= starting_capital * max_loss% / stop_loss%`). Each reduction is a passed check
with "reduced from X to Y"; if the result is below `min_order_notional` the verdict is blocked.

**Confirmation:** `requires_confirmation = approved and (control.confirmation_mode or symbol activation == CONFIRMATION)`.
Protective SELLs (`exit_reason` in STOP_LOSS / TAKE_PROFIT / TRAILING_STOP / MAX_LOSS / EMERGENCY_STOP, i.e. what
`check_exits` produces; see `is_risk_exit`) and any SELL during emergency stop never
wait for confirmation (`risk_exits_bypass_confirmation=True`). Strategy SELLs (e.g. RSI exit from the engine) do.

**Per-symbol rules:** `apply_manual_strategy(s)` / `set_symbol_rules(symbol, SymbolRiskRules(...))` override
`position_rules`, `exit_rules`, `activation` for one symbol; everything else uses `RiskConfig` defaults.

### `check_exits(portfolio, prices, bars=None) -> list[Signal]`
One SELL per position to close, with `exit_reason`, `position_id` (= `Position.id`), `price` = **fill price**,
`proposed_notional` = qty * fill, `reason` = full explanation, `conditions_met = [<ExitReason>]`, `strategy` = the
position's strategy, `source` MANUAL if strategy starts with `MANUAL` else AUTO, `timestamp` = bar time (or clock).
`bars` (symbol -> latest closed bar) turns on the intrabar rules below; symbols without a bar use `prices`, then
`Position.current_price`; positions with no price at all are skipped. **The portfolio is never mutated** and the manager
keeps no per-position state — same input, same output. `check_exits_candles(portfolio, candles)` is a deprecated alias
for `check_exits(portfolio, {}, bars=candles)`.
Stops are enforced even while paused. During **emergency stop every open position gets an `EMERGENCY_STOP` SELL** at the
latest price (candle close / price / `current_price` / entry). Levels: `Position.stop_loss_price` /
`take_profit_price` / `trailing_stop_pct` if set, else `entry * (1 -/+ pct)` from the symbol's `ExitRules`
(TP 12%, SL 6%, trailing 4% off by default). Max-loss-per-trade (OFF by default) adds a stop at
`entry - starting_capital * pct / 100 / quantity` (reason `MAX_LOSS`). RSI / MACD exits need indicators and are Bot 2's job.

#### Exit semantics (contract v0.2 `ExitRules` docstring + reference/README.md item 12; shared with Bot 2's `backtest_manual`)
1. **SL and TP use the bar's LOW / HIGH.** `low <= stop` -> stop loss; `high >= target` -> take profit.
   **If a stop and the TP are both hit in the same bar, the stop wins** (intrabar order unknown -> assume worst case).
   This applies to every stop (SL, trailing, max-loss), not only SL.
2. **Trailing stop:** the bar's LOW is compared with `trail = hwm_prev * (1 - pct/100)` where
   `hwm_prev = max(Position.highest_price, entry)` **as given** (prior bars only; strict `low < trail` like the simulator's
   `close < trail`). A bar's own high never tightens its own stop. The **app** then raises
   `highest_price = max(highest_price or entry, bar.high)` for still-open positions (risk does not store it). With
   `highest_price=None` the first trail is `entry * (1 - pct/100)`, as in the simulator.
3. **Fills at the level, not the close.** If the bar **opens beyond the level** (gap down through a stop, gap up through
   the target) the fill is the **open**.
4. Several stops in one bar: the highest level (touched first on the way down) gives the fill and reason; ties ->
   TRAILING_STOP > STOP_LOSS > MAX_LOSS (simulator priority).

**Versus the reference simulator (close-only):** the sim checks only `close` and fills at `close`, and raises the trail
with the close before checking. With `check_exits(prices)` each price is a degenerate bar (O=H=L=C), which reduces exactly
to the simulator's close check (raising-then-checking with the same close can never trigger, so the order doesn't matter);
fills are then the last price. With candles, wicks that touch a level trigger exits the sim would miss, and fills are at
the level/open instead of the close — so backtest PnL will differ from the sim (by design, more realistic).

Call `check_exits` **once per closed bar**, before entries, and not on the entry bar (the sim manages exits before
entries on each bar). Skipping bars loses wick information; if the app doesn't update `highest_price` the trailing stop
never ratchets.

### Controls
`pause(reason)` -> paused; `resume()` clears `paused` only, **never** an emergency stop; `emergency_stop(reason)` ->
`emergency_stopped=True` (`paused` untouched), blocks every BUY and makes `check_exits` emit an `EMERGENCY_STOP` SELL for
every position (closing SELLs still pass `check`); `reset_emergency_stop()` (interface method) clears it and leaves
`paused` as it was; `set_confirmation_mode(bool)`; `set_trading_mode(mode)` refuses LIVE unless
`RiskConfig.allow_live=True` (default False; without it `check` blocks every LIVE BUY, closing SELLs still pass). Every change sets
`reason` and `updated_at`.

### Notes for other bots
- **Bot 6 (loop):** per bar `check_exits(portfolio, prices, bars)` -> `check` each SELL -> close `position_id` ->
  update `highest_price` from `bar.high` for open positions ->
  `bars_since_exit[symbol] = 0` on exit and `+1` per bar, `daily_pnl`, `trades_today`, `equity`, `cash`; then engine ->
  `check(signal, portfolio, rm.get_control_state())` -> `adjusted_notional`; if `requires_confirmation` hold as
  `PENDING_CONFIRMATION` and re-run `check` at confirm time (state may have changed). Expose pause / resume /
  emergency_stop / reset_emergency_stop / set_confirmation_mode via the API.
- **Bot 1 (UI):** show `RiskVerdict.checks` as a pass/fail list with `detail`, `reasons[0]` as the headline, the
  control `reason`, a separate "Reset emergency stop" button, and `exit_levels()` (SL / TP / trail lines) per position.
- **Bot 2:** match the exit semantics above in `backtest_manual`.

---

## LIVE (real funds) profile — `live.py`

Applied **automatically** whenever the trading mode is LIVE (manager control state, the `control` passed to `check`,
or `Portfolio.mode`). SIM/PAPER are unaffected. The agent is **autonomous**: a LIVE BUY that passes every hard limit is
approved with `requires_confirmation=False`; confirmation is only requested if the user explicitly turns confirmation
mode on (or the strategy's activation is `CONFIRMATION`). Protective SELLs (SL/TP/trailing/max-loss/emergency) are never
blocked by a LIVE guard and never wait.

### Three switches (all required for a LIVE BUY)
| Switch | Owner | Effect |
|---|---|---|
| `LIVE_TRADING_ENABLED=true` | Bot 6 app | app refuses to run the LIVE loop otherwise |
| `BINANCE_LIVE_ENABLED=true` | Bot 4 exchange | LIVE exchange client cannot be built otherwise |
| `RiskConfig.allow_live=True` | Bot 3 risk | otherwise every LIVE BUY fails check `trading_mode` |

`DefaultRiskManager()` without `config=` uses `RiskConfig.from_env()`, which sets `allow_live=True` only when
`RISK_ALLOW_LIVE`, `LIVE_TRADING_ENABLED` and `BINANCE_LIVE_ENABLED` are all exactly `true`. An explicit
`RiskConfig(allow_live=True)` (tests / code) bypasses env. No secrets are read.

### `LiveRiskProfile` (`RiskConfig.live`) — defaults
| Field | Default | Env override (`from_env`) | Check name |
|---|---|---|---|
| `max_order_notional_usdt` | 25 | `RISK_LIVE_MAX_ORDER_USDT` | `live_order_cap` (reduces size) |
| `max_daily_loss_usdt` | 25 | `RISK_LIVE_MAX_DAILY_LOSS_USDT` | `live_daily_loss_cap` (**auto emergency stop**) |
| `max_daily_notional_usdt` | 100 | `RISK_LIVE_MAX_DAILY_NOTIONAL_USDT` | `live_daily_notional` (reduces size) |
| `max_open_exposure_usdt` | 50 | `RISK_LIVE_MAX_EXPOSURE_USDT` | `live_max_exposure` (reduces size) |
| `max_data_age_seconds` | 900 | `RISK_LIVE_MAX_DATA_AGE_S` | `live_stale_data` |
| `max_future_skew_seconds` | 60 | — | `live_stale_data` |
| `max_quote_age_seconds` | 30 (quote TTL) | — | `live_quote` |
| `max_slippage_pct` | 1.0 | `RISK_LIVE_MAX_SLIPPAGE_PCT` | `live_slippage` |
| `require_quote` | False | — | `live_quote` |
| `market_hours_enabled` / `market_open_utc` / `market_close_utc` / `market_weekdays_only` | False (24/7) / 13:30 / 20:00 / True | `RISK_LIVE_MARKET_HOURS=true` to enable | `live_market_hours` |
| `market_hours_exempt_symbols` | [] | — | `live_market_hours` |
| `require_wallet_balances` | True | — | `live_gas_reserve` |
| `quote_asset` / `fee_buffer_usdt` | USDT / 1.0 | `RISK_LIVE_FEE_BUFFER_USDT` | `live_wallet_balance` (reduces), `live_wallet_quote` |
| `gas_asset` / `min_gas_reserve` | BNB / 0.002 | `RISK_LIVE_MIN_BNB` | `live_gas_reserve` |

Size caps only **reduce** the order (each logged as "reduced from X to Y"); if it ends below `min_order_notional`
(10 USDT) the BUY is blocked. The other guards block outright.

Semantics:
- **Daily loss** = `-(Portfolio.daily_pnl + Σ unrealized)` where unrealized = `qty * (current_price - entry)` (or
  `Position.unrealized_pnl` if no `current_price`). Unrealized includes positions opened before today. When it reaches the
  cap, `check()` or `check_exits()` sets `emergency_stopped=True` with the reason
  "Auto emergency stop: LIVE daily loss X USDT reached the cap of Y USDT ...". `check_exits` then emits `EMERGENCY_STOP`
  SELLs for every position. Only `reset_emergency_stop()` clears it.
- **Daily notional** = LIVE BUY fills today (UTC). This is the larger of the internal ledger fed by `record_execution(result)`
  (LIVE, BUY, FILLED/PARTIALLY_FILLED only) and `LiveContext.daily_live_notional_usdt` (use this so the count survives
  restarts). SELLs don't count.
- **Stale data** uses `LiveContext.data_timestamp`, or `bar.timestamp + bar_interval` (bar close). `Candle.timestamp` is the
  bar OPEN time, so a 1h bar's open is always more than 900 s old: pass `data_timestamp` = when the price was fetched (or the bar
  close). Missing → blocked. Naive datetimes are treated as UTC.
- **Quote** (if supplied): symbol/side must match; it must not be expired or older than `max_quote_age_seconds`.
  `|quote.price - ref| / ref` and `quote.slippage_bps/100` must both be `<= max_slippage_pct` (ref =
  `context.reference_price` → `signal.price` → `context.bar.close`; none → blocked). `quote.notional` must be `<=` the approved
  size. With balances, `quote.notional + quote.fee <= stable balance - fee buffer`.
- **Market hours**: 13:30–20:00 UTC Mon–Fri matches the US session during EDT. During EST the session is 14:30–21:00 UTC, so
  adjust `market_open_utc`/`market_close_utc`. Exchange holidays are not modelled. Uses `context.now` or the clock.
- **Balances**: `{"USDT": 40.0, "BNB": 0.01}` (keys are case-insensitive). BUY notional is capped at `USDT - fee_buffer`. BNB must be
  `>= min_gas_reserve`. If no balances are given and `require_wallet_balances` is set → blocked.

### What the loop (Bot 6) must pass in LIVE
```python
ctx = LiveContext(now=utc_now(), data_timestamp=<price fetch time or bar close>, bar=last_bar, reference_price=last_price,
                  balances=wallet_balances,                 # e.g. from baw / exchange: {"USDT": .., "BNB": ..}
                  daily_live_notional_usdt=<persisted LIVE BUY notional today>)   # optional; ledger also used
v = risk.check(signal, portfolio, control, context=ctx)      # phase 1: size (adjusted_notional)
q = exchange.get_quote(sym, BUY, v.adjusted_notional)
v = risk.check(signal, portfolio, control, context=ctx.model_copy(update={"quote": q, "now": utc_now()}))  # phase 2
if v.approved: res = exchange.execute_swap(order, q); risk.record_execution(res)
risk.check_exits(portfolio, prices, bars)                    # every tick (also enforces the daily loss cap)
risk.live_limits(portfolio, ctx)                             # -> dict for GET /api/wallet "limits"
```
`live_limits()` returns `live_allowed, per_order_cap_usdt, daily_loss_cap_usdt, daily_notional_cap_usdt,
max_exposure_usdt, max_slippage_pct, stale_after_s, bnb_gas_reserve, fee_buffer_usdt, market_hours_utc,
daily_used_usdt, daily_loss_usdt, open_exposure_usdt`. The last two are `None` without a portfolio.
