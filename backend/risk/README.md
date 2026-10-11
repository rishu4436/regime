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
`RiskConfig.allow_live=True` (default False, and `check` blocks everything in LIVE without it). Every change sets
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
