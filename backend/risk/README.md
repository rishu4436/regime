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
| `BUY` | `signal_action`, `trading_mode` (LIVE blocked unless `allow_live`), `emergency_stop`, `paused`, `max_open_positions` (`len(positions) < max`, simulator gate; default **3**, see below), `cooldown` (`bars_since_exit[symbol] > cooldown_bars`, missing = no recent exit), `daily_loss_limit` (`daily_pnl > -X% * starting_capital`, default 5%; **skipped in LIVE**, see below), `max_trades_per_day` (off by default), then sizing: `position_size`, `cash_available`, `max_token_exposure` (default 30% of equity), `max_total_exposure` (default 60%), `max_loss_per_trade` (OFF by default), `min_order_notional` (default 10 USDT), and `confirmation` (if approved) |
| `SELL` | `signal_action`, `trading_mode`, `emergency_stop` (always passes — closing is allowed), `paused` (passes unless `allow_exits_when_paused=False`), `open_position` (must hold the symbol), `sell_size` (full position unless `proposed_notional` is smaller), `confirmation` |

**Sizing (BUY):** desired size = `signal.proposed_notional` if > 0, else `signal.proposed_size_pct` if > 0, else
`position_size_pct` (default 15%, simulator) — always capped at `position_size_pct` of **equity**. Then reduced (never
increased) by cash, per-token exposure, total exposure (exposure valued at `current_price`, else entry price) and, if
enabled, max-loss-per-trade (`notional <= starting_capital * max_loss% / stop_loss%`). Each reduction is a passed check
with "reduced from X to Y"; if the result is below `min_order_notional` the verdict is blocked (in LIVE it is first
raised to the minimum if every hard cap allows it - see "LIVE min-order bump").
**If a cap cut the order below the minimum, that cap is the reported reason** (its check fails, e.g.
`Blocked (max_token_exposure): NVDAB exposure limit reached: 3,000.00 of 3,000.00 USDT used ...`) and
`min_order_notional` passes with "blocked by <cap>". When a LIVE bump is refused, the tightest hard cap is reported.
`min_order_notional` itself fails only when nothing cut the order (the requested size was simply too small).

**Default max open positions = 3.** The contract's `PositionRiskRules.max_open_positions` defaults to 1. Risk uses
`RiskConfig.default_max_open_positions` (3) whenever the rules in force did not set the field **explicitly**
(pydantic `model_fields_set`) - for `RiskConfig.position_rules` and for a symbol / ManualStrategy override alike.
An explicit value (e.g. a ManualStrategy JSON carrying `"max_open_positions": 1`) always wins. Applied in `rules_for`.

**Confirmation:** `requires_confirmation = approved and control.confirmation_mode`. The **global toggle
(`ControlState.confirmation_mode`) takes priority**: on -> every entry / strategy SELL waits for the user, off -> none
does, whatever the strategy's `activation` says (activation is informational only).
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
| `max_slippage_pct` | 1.0 | `RISK_LIVE_MAX_SLIPPAGE_PCT` | `live_slippage` (max **adverse** impact + max quote slippage tolerance) |
| `max_favorable_impact_pct` | 3.0 | `RISK_LIVE_MAX_FAVORABLE_IMPACT_PCT` | `live_slippage` (sanity cap on a quote **better** than the reference) |
| `require_quote` | False | — | `live_quote` |
| `market_hours_enabled` / `market_open_utc` / `market_close_utc` / `market_weekdays_only` | False (24/7) / 13:30 / 20:00 / True | `RISK_LIVE_MARKET_HOURS=true` to enable | `live_market_hours` |
| `market_hours_exempt_symbols` | [] | — | `live_market_hours` |
| `require_wallet_balances` | True | — | `live_gas_reserve` |
| `quote_asset` / `fee_buffer_usdt` | USDT / 1.0 | `RISK_LIVE_FEE_BUFFER_USDT` | `live_wallet_balance` (reduces), `live_wallet_quote` |
| `gas_asset` / `min_gas_reserve` | BNB / 0.002 | `RISK_LIVE_MIN_BNB` | `live_gas_reserve` |
| `bump_to_min_order` | True | - | LIVE min-order bump (below) |
| `use_pct_daily_loss_in_live` | False | - | `daily_loss_limit` (the %-of-capital rule) in LIVE |

Size caps only **reduce** the order (each logged as "reduced from X to Y"); if it ends below `min_order_notional`
(10 USDT) the BUY is blocked - unless the LIVE min-order bump applies. The other guards block outright.

**LIVE min-order bump** (`bump_to_min_order=True`, LIVE only): if the sized BUY is below the effective minimum
`max(min_order_notional, token_status.min_order_usdt)`, it is raised to that minimum **if the minimum fits under every
hard ceiling**: cash, our per-order cap, wallet per-tx cap, wallet stablecoin balance minus `fee_buffer_usdt`, LIVE open
exposure room, our daily notional room, wallet daily quota and wallet exit reserve, and max-loss-per-trade if set.
The **%-of-equity rules** (`position_size_pct` 15%, `max_token_exposure_pct`, `max_total_exposure_pct`) may be overridden
by the bump; hard caps never are. Logged on `min_order_notional`: "Order raised from X to Y USDT to meet the NVDAon token
minimum ..." (also in `reasons`). If it doesn't fit: blocked with "cannot raise to Y USDT: a hard cap allows at most Z".
Effect: a 30 USDT wallet buys NVDAB at 10 (29 after the fee buffer), a 100 USDT wallet buys Ondo tokens at 20.

**%-of-capital daily loss in LIVE:** with `use_pct_daily_loss_in_live=False` (default) the `daily_loss_limit` check
passes in LIVE with "LIVE uses the daily loss cap of X USDT instead" - the absolute `max_daily_loss_usdt` cap governs.
SIM / PAPER unchanged.

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
  Price impact is directional (ref = `context.reference_price` → `signal.price` → `context.bar.close`; none → blocked):
  for a BUY, adverse = `(quote.price - ref) / ref` when positive; for a SELL, adverse = `(ref - quote.price) / ref` when
  positive (`quote_impact_pct`). Adverse impact must be `<= max_slippage_pct`. A favorable quote (cheaper BUY / richer
  SELL) is allowed up to `max_favorable_impact_pct` (3 %); beyond that it is blocked as "likely a bad quote".
  `quote.slippage_bps/100` must be `<= max_slippage_pct` either way. `quote.notional` must be `<=` the approved
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
Also `min_order_usdt`, `min_wallet_usdt` = smallest stablecoin balance at which a minimum-size LIVE BUY can pass
(`max(min_order_notional, ctx.token_status.min_order_usdt) + fee_buffer_usdt`, swap fee on top; `None` if that minimum
is above the effective per-order cap) and `min_wallet_usdt_note` (explains it, says when a token minimum was used).
`warnings` adds `wallet_too_small` (ctx balance below `min_wallet_usdt`) and `token_min_above_cap`. For the UI's
"wallet too small" banner.

### Wallet limits, token status, token minimums, blocked symbols (LIVE BUYs only)

SELLs (protective exits and strategy sells) and `check_exits` are **never** affected by anything in this section.

**Wallet limits — `LiveContext.wallet_limits: WalletLimits`.** The Agentic Wallet enforces its own spending controls
(set in the Binance App, read-only from `baw`). Risk applies whichever is **stricter**, ours (`LiveRiskProfile`) or the
wallet's, so a BUY is shrunk/blocked before the wallet would reject it.

| Check | Effect |
|---|---|
| `wallet_limits` | informational pass "Wallet limits not supplied - only our own LIVE limits apply"; **blocks** if `LiveRiskProfile.require_wallet_limits=True` (default False) |
| `wallet_allowed_token` | blocks when `allowed_tokens` is set and the symbol (or `token_address` on the signal/quote, if present) is not in it; case-insensitive |
| `wallet_per_tx_cap` | reduces size to `per_tx_limit_usdt` |
| `wallet_daily_limit` | reduces size to `min(daily_limit - daily_used, daily_left)`, floored at 0 |
| `live_quote_notional` | quote notional must be ≤ approved size and ≤ the **effective** per-order cap |

`min_order_notional` still runs afterwards, so a wallet-shrunk order under the minimum is blocked.

Fill it from `baw wallet settings --json` (envelope or `data`):
```python
wl = WalletLimits.from_baw(json.loads(out), fetched_at=utc_now(), allowed_tokens=cfg_allow_list_or_None)
ctx = LiveContext(..., wallet_limits=wl)
```
Mapping (source: official `wallet settings` reference,
<https://github.com/binance/binance-skills-hub/blob/main/skills/binance-web3/binance-agentic-wallet/references/wallet-setting.md>,
also listed in Bot 4's `binance-research.md` §3):

| baw key (documented) | WalletLimits field |
|---|---|
| `dailyLimit` | `daily_limit_usdt` (baw reports USD; treated as USDT 1:1) |
| `quotaUsed` / `quotaLeft` / `quotaDate` | `daily_used_usdt` / `daily_left_usdt` / `quota_date` |
| `tradeAllTokens` | `trade_all_tokens` (True → `allowed_tokens=None`) |

**Provisional (NOT backed by any doc):** `baw` documents **no per-transaction limit** and does **not return the token
allow-list** (only `tradeAllTokens: false`). `from_baw` tolerates aliases such as `perTxLimit`, `perTransactionLimit`,
`singleTxLimit`, `allowedTokens`, `tokenWhitelist`, `allowedContracts` in case a CLI version or wrapper returns them; otherwise
pass `allowed_tokens=` explicitly. With `tradeAllTokens=false` and no list, `wallet_allowed_token` passes with a "not
verified" note (the wallet itself will still reject). Prediction/DeFi/x402/dev-mode quotas are separate and ignored.
Bot 4's dedicated `agentic-wallet-deep-research.md` did not exist yet when this was written.

`live_limits(portfolio, ctx)` reports **effective** values: `per_order_cap_usdt`, `daily_notional_cap_usdt` and
`daily_limit_usdt` (same value), `daily_used_usdt` (from the side that binds), `daily_remaining_usdt`, plus
`limit_source: {per_order_cap_usdt, daily_limit_usdt}` = `"risk"` or `"wallet"`, `allowed_tokens`, `risk_limits` (ours,
raw) and `wallet_limits` (raw wallet values, or None).

**Token status — `LiveContext.token_status: TokenStatus`** (`trading_status`, `min_order_usdt`, `reference_price_only`;
a plain dict works). Check `live_token_status` blocks unless `trading_status == "trading"` and `reference_price_only` is
False, e.g. "NVDAon is off-hours - price is reference only - no LIVE BUY". Missing → informational pass.

**Token minimum order:** in LIVE the minimum is `max(min_order_notional (10), token_status.min_order_usdt)` (Ondo: 20 USD).
Below it the LIVE bump raises the order to it when the hard caps allow; otherwise `min_order_notional` fails with
"below token minimum order".

**Blocked symbols:** `LiveRiskProfile.blocked_symbol_suffixes = ["x"]` (case-sensitive `endswith`). Check
`live_symbol_allowed` blocks e.g. `TSLAx` (xStocks have no liquidity on BNB Chain); `NVDAX`/`NVDAon` pass. Set `[]` to disable.

**Exit-quota reserve (wallet `dailyLimit` counts SELLs too, per Bot 4).** With a wallet daily limit/quota known and
`LiveRiskProfile.reserve_wallet_quota_for_exits=True` (default), check `wallet_exit_reserve` shrinks a LIVE BUY to
`N <= (quota_left - open_exposure) / 2`, so after buying, the remaining quota can still sell every open position including
this one ("keeping X USDT of it to sell open positions ..."). Then `min_order_notional` blocks if too small. SELLs untouched.
`live_limits()` adds `exit_quota_shortfall_usdt` = `max(0, open_exposure - quota_left)` (None if portfolio or wallet quota
unknown) and `warnings: [{code, message}]` (always present, `[]` = none): `exit_quota_shortfall`, and
`wallet_limits_stale` when `wallet_limits.fetched_at` is older than 15 min (vs `context.now` or the clock).

**Exchange price impact.** `LiveContext.price_impact_pct` (Web3 quote `priceImpactPercent`, in %, sign ignored: Binance
does not document the sign, only positive samples were seen, and Bot 4's `exchange/status.py` passes the route value
through unchanged but computes its own fallback as positive = worse for us, so the two can't be told apart; the magnitude
is counted as adverse against `max_slippage_pct`, and the favorable allowance applies only to the quote-vs-reference path) — or a
price-impact attribute on `Quote` if the contract ever adds one — replaces the quote-vs-reference comparison in
`live_slippage` ("Exchange-reported price impact ..."). Without it the old reference-price fallback runs. The quote's
`slippage_bps` tolerance is checked either way.

## Runtime trading limits (dashboard "Trading limits" panel) — Bot 3

Live limits can be changed while Regime runs, with no restart. The defaults are unchanged (25 USDT per order,
50 USDT total, 25 USDT daily loss, 100 USDT daily buys), so nothing changes for existing users.

```python
rm.live_limits_settings(wallet_limits: WalletLimits | None = None) -> dict          # GET
rm.set_live_limits(settings: Mapping, wallet_limits: WalletLimits | None = None) -> dict   # PUT, raises ValueError
rm.reset_live_limits(wallet_limits: WalletLimits | None = None) -> dict             # "Reset to safe defaults"
```

All three return the same shape:

```json
{"order_mode": "fixed"|"pct", "order_usdt": null|float, "order_pct": null|float,
 "max_order_usdt": 25, "max_total_usdt": 50, "max_per_token_usdt": 50, "daily_loss_usdt": 25, "daily_buy_usdt": 100,
 "defaults": {same 8 keys, the values this manager started with (env RISK_LIVE_* included)},
 "wallet_daily_limit_usdt": null|float,   // wallet_limits.daily_limit_usdt when passed
 "notes": ["..."]}                        // e.g. "Daily buy limit raised from 100.00 to 800.00 USDT ..."
```

| Panel key | LiveRiskProfile field | Notes |
|---|---|---|
| `order_mode` | `order_mode` | `fixed` (default) or `pct` |
| `order_usdt` | `order_usdt` | fixed USDT per LIVE BUY; `null` = use the strategy's size (today's behaviour) |
| `order_pct` | `order_pct` | % of the wallet stablecoin per LIVE BUY, (0, 100]; required in `pct` mode |
| `max_order_usdt` | `max_order_notional_usdt` | per-order cap |
| `max_total_usdt` | `max_open_exposure_usdt` | total open positions incl. the new order |
| `max_per_token_usdt` | `max_token_exposure_usdt` (new, LIVE only) | stored `null` = same as `max_total_usdt` |
| `daily_loss_usdt` | `max_daily_loss_usdt` | hitting it triggers the automatic emergency stop |
| `daily_buy_usdt` (optional) | `max_daily_notional_usdt` | if omitted and `max_order_usdt` rises above it, raised to 4 × `max_order_usdt` (reported in `notes`) |

**Validation** (all or nothing: on any error nothing changes; `ValueError` with every problem in plain English,
`'; '`-joined → map to HTTP 400): amounts finite and > 0; `order_pct` in (0, 100]; `max_order_usdt <= max_total_usdt`;
`max_per_token_usdt <= max_total_usdt`; in fixed mode `order_usdt <= max_order_usdt`; `daily_buy_usdt >= max_order_usdt`;
when `wallet_limits.daily_limit_usdt` is known, `max_order_usdt` may not exceed it. Unknown keys are rejected;
missing keys keep their value (partial PUT works); `defaults`, `wallet_daily_limit_usdt` and `notes` echoed back from
GET are ignored. `live_limits()` (the /api/wallet info) reflects changes at once and now also has
`max_token_exposure_usdt`, `order_mode`, `order_usdt`, `order_pct`. `set_config()` replaces the whole config including
these runtime values.

**Order sizing (LIVE BUYs only; SIM/PAPER unchanged).** `pct`: target = wallet stablecoin balance
(`LiveContext.balances[quote_asset]`, or `portfolio.cash` if balances are missing) × `order_pct` / 100. `fixed` with
`order_usdt`: target = `order_usdt`. The target replaces the strategy / %-of-equity size, then every existing cap still
clamps it: cash, per-token % and total % of equity, per-order cap, LIVE total and per-token exposure, LIVE daily buys,
wallet per-tx, wallet daily quota and exit reserve, wallet balance − fee buffer; `bump_to_min_order` still applies.
Example: 10,000 USDT wallet at 2% = 200 USDT, but with the default caps it's clamped to 25.

### How Bot 6 wires `GET/PUT /api/settings/limits`
```python
wl = wallet_settings.get()                         # WalletLimits or None
GET:  return rm.live_limits_settings(wl)
PUT:  try: out = rm.set_live_limits(body, wl)
      except ValueError as e: raise HTTPException(400, str(e))
      engine.set_live_limits(max_order_usdt=out["max_order_usdt"],                 # Bot 2, same values
                             max_size_pct=out["order_pct"] if out["order_mode"] == "pct" else None)
      exchange per-order cap: raise BINANCE_LIVE_MAX_NOTIONAL (Bot 4) to >= out["max_order_usdt"], or the exchange
      still rejects orders above 25 USDT. Settings.live_max_order_usdt (app fallback) should follow too.
      persist out (minus defaults/notes) to logs/ and call rm.set_live_limits(saved, wl) again at startup.
RESET: out = rm.reset_live_limits(wl); same engine / exchange calls.
```

## Watchlist: caps across symbols and same-tick reservations — Bot 3

Every cap already counts all symbols together: `max_open_positions` = `len(portfolio.positions)` (all symbols),
total exposure (% of equity and LIVE USDT) sums all positions, per-token caps sum that symbol's positions, and the
LIVE daily buy cap counts every LIVE BUY (in-flight `PENDING_CONFIRMATION` ones via `record_execution`). In-flight chain
buys count because the app passes them as synthetic positions.

**The gap:** when several symbols are checked in one tick against the same `portfolio`, each check sees the full room,
so two BUYs could together exceed a cap. **Fix: reservations.**

```python
rm.check(signal, pf, control, context=ctx, reserve_as=key)  # approved BUY -> holds adjusted_notional under key
rm.release_reservation(key) -> bool
rm.reserve_buy(key, symbol, notional, ttl_seconds=None)      # manual
rm.clear_reservations() -> int
rm.reservations() -> [{"key", "symbol", "notional_usdt", "expires_at"}]
```
Each later check counts every other unexpired reservation toward open positions (+1 each), cash, per-token and total
exposure, LIVE daily buys, wallet quota/exit reserve and wallet balance (check `reserved_orders` explains it). A re-check
with the same key (LIVE pass 2 with the quote, approval re-check) excludes and replaces its own reservation; a blocked
re-check drops it. `record_execution(result)` also releases the reservation whose key == `result.client_order_id`.
Expiry after `RESERVATION_TTL_S` (900 s) is only a safety net.

Loop pattern for Bot 6 (key = the decision id):
```python
for sig in signals:                                   # one per watchlist token, sells first
    did = new decision id
    v = rm.check(sig, self._pf(), control, context=ctx, reserve_as=did)
    if v.approved and v.requires_confirmation:  keep the reservation; release on approve/reject/expire
    elif v.approved:  self._buy(...) with the same did (pass 2: reserve_as=did); then rm.release_reservation(did)
# approve(did): v = rm.check(..., reserve_as=did) -> _buy -> rm.release_reservation(did)
# reject/expire(did), agent switch: rm.release_reservation(did) / rm.clear_reservations()
```
Release only once the order is in the portfolio you pass next (filled lot or in-flight synthetic position), or will
not happen; rebuild the portfolio (`self._pf()`) for each symbol so it's never counted twice.
