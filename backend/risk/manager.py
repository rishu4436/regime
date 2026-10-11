"""Bot 3 - Risk & Control layer. ``DefaultRiskManager`` implements ``backend.contracts.RiskManager``.

Deterministic, no network, no secrets. Inject ``clock`` in tests. See backend/risk/README.md for the full design.
"""
from __future__ import annotations

import threading
from datetime import date, datetime
from typing import Callable, Optional

from backend.contracts import (
    Action,
    ActivationMode,
    AgentMode,
    Candle,
    ControlState,
    ExecutionResult,
    ExitReason,
    OrderSide,
    OrderStatus,
    Portfolio,
    Position,
    RiskCheck,
    RiskVerdict,
    Signal,
    TradingMode,
    utc_now,
)

from .config import RiskConfig, SymbolRiskRules
from .live import LiveContext, as_utc, in_market_hours
from .exits import Bar, ExitLevels, compute_levels, evaluate_bar, max_loss_level

# Exit reasons produced by the risk layer itself (protective exits that never wait for user confirmation).
RISK_EXIT_REASONS = frozenset({ExitReason.STOP_LOSS, ExitReason.TAKE_PROFIT, ExitReason.TRAILING_STOP,
                               ExitReason.MAX_LOSS, ExitReason.EMERGENCY_STOP})


def _fmt(x: float) -> str:
    return f"{x:,.2f}"


def is_risk_exit(signal: Signal) -> bool:
    """True for a protective SELL as produced by ``check_exits`` (exit_reason in RISK_EXIT_REASONS)."""
    return signal.action == Action.SELL and signal.exit_reason in RISK_EXIT_REASONS


class DefaultRiskManager:
    """Risk gate + exit enforcement + global controls. Thread-safe. The only in-memory state is the ControlState
    (and config); positions, trailing high-water marks (Position.highest_price), cooldowns and PnL are read from the
    Portfolio passed in, which is never mutated."""

    def __init__(
        self,
        control: ControlState | None = None,
        config: RiskConfig | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._clock = clock
        self._config = config if config is not None else RiskConfig.from_env()
        self._control = control or ControlState(updated_at=clock())
        self._lock = threading.RLock()
        self._live_bought: dict[date, float] = {}  # UTC day -> LIVE BUY notional filled (from record_execution)

    # ------------------------------------------------------------------ configuration
    @property
    def config(self) -> RiskConfig:
        return self._config

    def set_config(self, config: RiskConfig) -> None:
        with self._lock:
            self._config = config

    def set_symbol_rules(self, symbol: str, rules: SymbolRiskRules | None) -> None:
        """Per-symbol override; ``None`` removes it."""
        with self._lock:
            sr = dict(self._config.symbol_rules)
            if rules is None:
                sr.pop(symbol, None)
            else:
                sr[symbol] = rules
            self._config = self._config.model_copy(update={"symbol_rules": sr})

    def apply_manual_strategy(self, strategy) -> None:  # strategy: ManualStrategy
        """Use a ManualStrategy's ``risk`` / ``exit`` / ``activation`` for its symbol."""
        self.set_symbol_rules(strategy.symbol, SymbolRiskRules.from_manual_strategy(strategy))

    # ------------------------------------------------------------------ controls
    def get_control_state(self) -> ControlState:
        with self._lock:
            return self._control

    def _update(self, **kw) -> ControlState:
        with self._lock:
            self._control = self._control.model_copy(update={**kw, "updated_at": self._clock()})
            return self._control

    def pause(self, reason: str = "") -> ControlState:
        return self._update(paused=True, reason=reason or "Paused by user")

    def resume(self) -> ControlState:
        """Clears pause. Does NOT clear an emergency stop (use ``reset_emergency_stop``)."""
        with self._lock:
            if self._control.emergency_stopped:
                return self._update(paused=False, reason=(
                    "Pause cleared, but EMERGENCY STOP is still active - call reset_emergency_stop() to trade again"))
            return self._update(paused=False, reason="Resumed")

    def emergency_stop(self, reason: str = "") -> ControlState:
        """Sets emergency_stopped=True (``paused`` untouched): every BUY is blocked and check_exits closes all positions."""
        return self._update(emergency_stopped=True,
                            reason=reason or "Emergency stop by user")

    def reset_emergency_stop(self) -> ControlState:
        """Explicit user action: clears emergency_stopped and leaves ``paused`` unchanged."""
        with self._lock:
            paused = self._control.paused
            return self._update(emergency_stopped=False, reason=(
                "Emergency stop reset - agent is still paused, call resume() to trade" if paused
                else "Emergency stop reset - trading allowed"))

    def set_confirmation_mode(self, enabled: bool) -> ControlState:
        return self._update(confirmation_mode=bool(enabled),
                            reason="Confirmation mode ON: every entry waits for the user" if enabled
                            else "Confirmation mode OFF: approved entries auto-execute")

    def set_trading_mode(self, mode: TradingMode) -> ControlState:
        """Switch SIM/PAPER/LIVE. LIVE is refused (state unchanged) unless ``RiskConfig.allow_live``."""
        with self._lock:
            if mode == TradingMode.LIVE and not self._config.allow_live:
                return self._update(reason="LIVE trading refused: not enabled in risk config (allow_live=False)")
            return self._update(trading_mode=mode, reason=f"Trading mode set to {mode.value}")

    def _effective_control(self, control: ControlState | None) -> ControlState:
        """Most restrictive combination of the manager's own state and the one passed by the caller."""
        own = self.get_control_state()
        if control is None:
            return own
        live = TradingMode.LIVE in (own.trading_mode, control.trading_mode)
        return own.model_copy(update=dict(
            paused=own.paused or control.paused,
            emergency_stopped=own.emergency_stopped or control.emergency_stopped,
            confirmation_mode=own.confirmation_mode or control.confirmation_mode,
            trading_mode=TradingMode.LIVE if live else control.trading_mode,
            reason=control.reason or own.reason,
        ))

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _position_value(p: Position, price: Optional[float] = None) -> float:
        return p.quantity * (price or p.current_price or p.avg_entry_price)

    @staticmethod
    def _equity(portfolio: Portfolio) -> float:
        return portfolio.equity if portfolio.equity > 0 else 0.0

    @staticmethod
    def _hwm_for(p: Position) -> float:
        """High-water mark from PRIOR bars only: Position.highest_price as given (the app raises it from bar.high
        after check_exits), never below the entry price."""
        return max(p.highest_price or 0.0, p.avg_entry_price)

    def exit_levels(self, position: Position, portfolio: Portfolio | None = None) -> ExitLevels:
        """Stop / target / trailing / max-loss levels currently in force for a position (for the UI)."""
        prules, erules, _ = self._config.rules_for(position.symbol)
        capital = portfolio.starting_capital if portfolio else 0.0
        return compute_levels(position, erules, self._hwm_for(position),
                              max_loss_level(position, prules.max_loss_per_trade_pct, capital))

    # ------------------------------------------------------------------ check
    def check(self, signal: Signal, portfolio: Portfolio, control: ControlState,
              context: Optional[LiveContext] = None) -> RiskVerdict:
        """``context`` (optional, Bot 3 extension) carries LIVE facts: now, data_timestamp/bar, quote, balances,
        reference_price, daily_live_notional_usdt. In LIVE, missing facts make the matching guard fail safe."""
        with self._lock:
            if self._is_live(control, portfolio):
                self._enforce_daily_loss_cap(portfolio, context)
            ctl = self._effective_control(control)
            if signal.action == Action.STAY_OUT:
                return self._verdict(signal, False, [RiskCheck(
                    name="signal_action", passed=False,
                    detail=f"Signal is STAY_OUT - nothing to execute ({signal.reason})")], 0.0, 0.0, False)
            if signal.action == Action.SELL:
                return self._check_sell(signal, portfolio, ctl)
            return self._check_buy(signal, portfolio, ctl, context or LiveContext())

    def _is_live(self, control: ControlState | None, portfolio: Portfolio) -> bool:
        return TradingMode.LIVE in (self._control.trading_mode, portfolio.mode,
                                    control.trading_mode if control else self._control.trading_mode)

    def _mode_check(self, ctl: ControlState, action: Action = Action.BUY) -> RiskCheck:
        if ctl.trading_mode == TradingMode.LIVE and not self._config.allow_live:
            if action == Action.SELL:
                return RiskCheck(name="trading_mode", passed=True,
                                 detail="LIVE disabled for new entries (allow_live=False), but closing positions is allowed")
            return RiskCheck(name="trading_mode", passed=False, detail=(
                "LIVE BUYs are disabled: risk config allow_live=False (needs RISK_ALLOW_LIVE, LIVE_TRADING_ENABLED and "
                "BINANCE_LIVE_ENABLED all = true)"))
        return RiskCheck(name="trading_mode", passed=True, detail=f"Trading mode {ctl.trading_mode.value} allowed")

    # ------------------------------------------------------------------ LIVE helpers
    def _now(self, context: Optional[LiveContext]) -> datetime:
        return context.resolved_now(self._clock()) if context else as_utc(self._clock())

    @staticmethod
    def _unrealized(p: Position) -> float:
        if p.current_price:
            return p.quantity * (p.current_price - p.avg_entry_price)
        return p.unrealized_pnl

    def daily_loss_usdt(self, portfolio: Portfolio) -> float:
        """Today's loss as a positive number: -(Portfolio.daily_pnl [realized today] + unrealized PnL of open positions)."""
        return max(0.0, -(portfolio.daily_pnl + sum(self._unrealized(p) for p in portfolio.positions)))

    def daily_live_notional_used(self, context: Optional[LiveContext] = None, now: Optional[datetime] = None) -> float:
        """LIVE BUY notional filled today (UTC): max of the internal ledger and context.daily_live_notional_usdt."""
        day = as_utc(now or self._now(context)).date()
        with self._lock:
            used = self._live_bought.get(day, 0.0)
        if context and context.daily_live_notional_usdt is not None:
            used = max(used, context.daily_live_notional_usdt)
        return used

    def record_execution(self, result: ExecutionResult) -> None:
        """Feed every ExecutionResult here; LIVE BUY fills count towards the daily notional cap (keyed by UTC day)."""
        if result.mode != TradingMode.LIVE or result.side != OrderSide.BUY:
            return
        if result.status not in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
            return
        day = as_utc(result.timestamp).date()
        with self._lock:
            self._live_bought[day] = self._live_bought.get(day, 0.0) + result.notional
            for d in [d for d in self._live_bought if d < day]:
                del self._live_bought[d]

    def _enforce_daily_loss_cap(self, portfolio: Portfolio, context: Optional[LiveContext]) -> None:
        cap = self._config.live.max_daily_loss_usdt
        loss = self.daily_loss_usdt(portfolio)
        if loss >= cap and not self._control.emergency_stopped:
            self._update(emergency_stopped=True, reason=(
                f"Auto emergency stop: LIVE daily loss {_fmt(loss)} USDT reached the cap of {_fmt(cap)} USDT "
                "(realized today + unrealized); all positions are being closed. Reset requires reset_emergency_stop()."))

    def live_limits(self, portfolio: Optional[Portfolio] = None, context: Optional[LiveContext] = None) -> dict:
        """Active LIVE limits for the UI (/api/wallet). Usage fields are None when the inputs are not supplied."""
        lp = self._config.live
        eff = self._effective_caps(context)
        wl = context.wallet_limits if context else None
        return {
            "live_allowed": self._config.allow_live,
            "per_order_cap_usdt": eff["per_order"],
            "daily_loss_cap_usdt": lp.max_daily_loss_usdt,
            "daily_notional_cap_usdt": eff["daily_cap"],
            "daily_limit_usdt": eff["daily_cap"],
            "daily_remaining_usdt": eff["daily_left"],
            "allowed_tokens": wl.allowed_tokens if wl else None,
            "blocked_symbol_suffixes": list(lp.blocked_symbol_suffixes),
            "limit_source": {"per_order_cap_usdt": eff["per_order_src"], "daily_limit_usdt": eff["daily_src"]},
            "risk_limits": {"per_order_cap_usdt": lp.max_order_notional_usdt,
                            "daily_notional_cap_usdt": lp.max_daily_notional_usdt,
                            "daily_used_usdt": self.daily_live_notional_used(context)},
            "wallet_limits": None if wl is None else {
                "per_tx_limit_usdt": wl.per_tx_limit_usdt, "daily_limit_usdt": wl.daily_limit_usdt,
                "daily_used_usdt": wl.daily_used_usdt, "daily_left_usdt": wl.daily_remaining_usdt(),
                "quota_date": wl.quota_date, "trade_all_tokens": wl.trade_all_tokens,
                "allowed_tokens": wl.allowed_tokens, "allowed_contracts": wl.allowed_contracts,
                "source": wl.source, "fetched_at": wl.fetched_at.isoformat() if wl.fetched_at else None},
            "max_exposure_usdt": lp.max_open_exposure_usdt,
            "max_slippage_pct": lp.max_slippage_pct,
            "stale_after_s": lp.max_data_age_seconds,
            "bnb_gas_reserve": lp.min_gas_reserve,
            "fee_buffer_usdt": lp.fee_buffer_usdt,
            "market_hours_utc": (f"{lp.market_open_utc:%H:%M}-{lp.market_close_utc:%H:%M}"
                                 + (" Mon-Fri" if lp.market_weekdays_only else "")) if lp.market_hours_enabled else None,
            "daily_used_usdt": eff["daily_used"],
            "exit_quota_shortfall_usdt": self.exit_quota_shortfall_usdt(portfolio, context),
            "warnings": self._limit_warnings(portfolio, context),
            "daily_loss_usdt": self.daily_loss_usdt(portfolio) if portfolio is not None else None,
            "open_exposure_usdt": sum(self._position_value(p) for p in portfolio.positions) if portfolio is not None else None,
        }

    def exit_quota_shortfall_usdt(self, portfolio: Optional[Portfolio], context: Optional[LiveContext]) -> Optional[float]:
        """How much of the open exposure the wallet could NOT sell today: max(0, open exposure - wallet quota left).
        None when the portfolio or the wallet's daily quota is unknown."""
        wl = context.wallet_limits if context else None
        left = wl.daily_remaining_usdt() if wl else None
        if portfolio is None or left is None:
            return None
        return max(0.0, sum(self._position_value(p) for p in portfolio.positions) - left)

    WALLET_LIMITS_STALE_S = 900.0

    def _limit_warnings(self, portfolio: Optional[Portfolio], context: Optional[LiveContext]) -> list[dict]:
        """UI warnings for live_limits(): [{"code", "message"}]; empty list = nothing to warn about."""
        out: list[dict] = []
        wl = context.wallet_limits if context else None
        short = self.exit_quota_shortfall_usdt(portfolio, context)
        if short and short > 0:
            left = wl.daily_remaining_usdt() or 0.0
            out.append({"code": "exit_quota_shortfall", "message": (
                f"Wallet daily quota left ({_fmt(left)} USDT) is below open positions ({_fmt(left + short)} USDT) - "
                "not every position can be sold today")})
        if wl is not None and wl.fetched_at is not None:
            age = (self._now(context) - as_utc(wl.fetched_at)).total_seconds()
            if age > self.WALLET_LIMITS_STALE_S:
                out.append({"code": "wallet_limits_stale", "message": (
                    f"Wallet limits were last read {age / 60:.0f} min ago (over {self.WALLET_LIMITS_STALE_S / 60:.0f} min) - "
                    "re-read them from the wallet")})
        return out

    def _live_buy_gates(self, signal: Signal, portfolio: Portfolio, ctx: LiveContext, checks: list[RiskCheck]) -> None:
        """LIVE-only hard gates that do not depend on the order size."""
        lp = self._config.live
        now = self._now(ctx)
        loss = self.daily_loss_usdt(portfolio)
        checks.append(RiskCheck(name="live_daily_loss_cap", passed=loss < lp.max_daily_loss_usdt, detail=(
            f"Today's LIVE loss {_fmt(loss)} USDT (realized + unrealized), cap {_fmt(lp.max_daily_loss_usdt)} USDT"
            + ("" if loss < lp.max_daily_loss_usdt else " - cap hit, emergency stop triggered"))))

        data_t = ctx.resolved_data_time()
        if data_t is None:
            checks.append(RiskCheck(name="live_stale_data", passed=False,
                                    detail="No price/bar timestamp supplied - cannot prove market data is fresh"))
        else:
            age = (now - data_t).total_seconds()
            if age < -lp.max_future_skew_seconds:
                checks.append(RiskCheck(name="live_stale_data", passed=False,
                                        detail=f"Market data timestamp is {-age:.0f}s in the future - clock or feed problem"))
            else:
                ok = age <= lp.max_data_age_seconds
                checks.append(RiskCheck(name="live_stale_data", passed=ok, detail=(
                    f"Latest market data is {max(age, 0):.0f}s old, limit {lp.max_data_age_seconds:.0f}s"
                    + ("" if ok else " - data is stale"))))

        if not lp.market_hours_enabled:
            checks.append(RiskCheck(name="live_market_hours", passed=True, detail="Market-hours guard disabled"))
        elif signal.symbol in lp.market_hours_exempt_symbols:
            checks.append(RiskCheck(name="live_market_hours", passed=True, detail=f"{signal.symbol} is exempt from market hours"))
        else:
            ok, why = in_market_hours(now, lp)
            checks.append(RiskCheck(name="live_market_hours", passed=ok, detail=why + ("" if ok else " - no new LIVE entries")))

        sym = signal.symbol
        bad = next((x for x in lp.blocked_symbol_suffixes if x and sym.endswith(x)), None)
        checks.append(RiskCheck(name="live_symbol_allowed", passed=bad is None, detail=(
            f"{sym} ends with blocked suffix '{bad}' (e.g. xStocks: no liquidity on BNB Chain) - not bought in LIVE"
            if bad else f"{sym} is not on a blocked suffix list {lp.blocked_symbol_suffixes}")))

        ts = ctx.token_status
        if ts is None:
            checks.append(RiskCheck(name="live_token_status", passed=True,
                                    detail=f"No token status supplied for {sym} - tradability not verified"))
        else:
            st = ts.trading_status.strip().lower()
            if st != "trading":
                why = {"offhours": "is off-hours", "closed": "market is closed", "no_liquidity": "has no liquidity"}.get(
                    st, f"status is '{ts.trading_status}'")
                det = f"{sym} {why}" + (" - price is reference only" if ts.reference_price_only else "") + " - no LIVE BUY"
            elif ts.reference_price_only:
                det = f"{sym} price is reference only - not executable, no LIVE BUY"
            else:
                det = f"{sym} is trading"
            checks.append(RiskCheck(name="live_token_status", passed=ts.is_trading(), detail=det))

        wl = ctx.wallet_limits
        if wl is None:
            checks.append(RiskCheck(name="wallet_limits", passed=not lp.require_wallet_limits, detail=(
                "No wallet limits supplied - require_wallet_limits is on, LIVE BUY blocked" if lp.require_wallet_limits
                else "Wallet limits not supplied - only our own LIVE limits apply")))
        else:
            checks.append(RiskCheck(name="wallet_limits", passed=True, detail=(
                f"Wallet limits from {wl.source or 'wallet'}: per-tx {self._opt(wl.per_tx_limit_usdt)}, daily "
                f"{self._opt(wl.daily_limit_usdt)} (used {self._opt(wl.daily_used_usdt)}) - stricter of wallet and ours applies")))
            q = ctx.quote
            ids = (sym, getattr(signal, "token_address", None), getattr(q, "token_address", None),
                   getattr(q, "contract_address", None))
            if wl.allowed_tokens is None:
                checks.append(RiskCheck(name="wallet_allowed_token", passed=True, detail=(
                    "Wallet trades only an App-side allow-list that baw does not return - not verified here"
                    if wl.trade_all_tokens is False else "Wallet has no token allow-list")))
            else:
                ok = wl.allows(*ids)
                checks.append(RiskCheck(name="wallet_allowed_token", passed=ok, detail=(
                    f"{sym} is on the wallet's allowed tokens" if ok
                    else f"{sym} is not on the wallet's allowed tokens - the wallet would reject it")))

        if ctx.balances is None:
            checks.append(RiskCheck(name="live_gas_reserve", passed=not lp.require_wallet_balances, detail=(
                "No wallet balances supplied - cannot verify stablecoin and BNB gas" if lp.require_wallet_balances
                else "No wallet balances supplied - balance checks skipped")))
        else:
            bnb = self._balance(ctx, lp.gas_asset)
            ok = bnb >= lp.min_gas_reserve
            checks.append(RiskCheck(name="live_gas_reserve", passed=ok, detail=(
                f"{lp.gas_asset} balance {bnb:g}, reserve for gas {lp.min_gas_reserve:g}" + ("" if ok else " - not enough gas"))))

    @staticmethod
    def _exchange_impact(ctx: LiveContext) -> Optional[float]:
        """Price impact % from LiveContext.price_impact_pct, else from the Quote if it ever carries such a field."""
        if ctx.price_impact_pct is not None:
            return float(ctx.price_impact_pct)
        for attr in ("price_impact_pct", "price_impact_percent", "priceImpactPercent"):
            v = getattr(ctx.quote, attr, None) if ctx.quote is not None else None
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    return None
        return None

    @staticmethod
    def _opt(x: Optional[float]) -> str:
        return "n/a" if x is None else f"{_fmt(x)} USDT"

    def _effective_caps(self, ctx: Optional[LiveContext]) -> dict:
        """Stricter of LiveRiskProfile and wallet limits: per-order cap and daily headroom (+ which side set each)."""
        lp = self._config.live
        wl = ctx.wallet_limits if ctx else None
        used = self.daily_live_notional_used(ctx)
        per, per_src = lp.max_order_notional_usdt, "risk"
        if wl and wl.per_tx_limit_usdt is not None and wl.per_tx_limit_usdt < per:
            per, per_src = wl.per_tx_limit_usdt, "wallet"
        ours_left = max(0.0, lp.max_daily_notional_usdt - used)
        d = dict(per_order=per, per_order_src=per_src, daily_cap=lp.max_daily_notional_usdt, daily_used=used,
                 daily_left=ours_left, daily_src="risk")
        wleft = wl.daily_remaining_usdt() if wl else None
        if wleft is not None and wleft < ours_left:
            cap = wl.daily_limit_usdt if wl.daily_limit_usdt is not None else (wl.daily_used_usdt or 0.0) + wleft
            d.update(daily_cap=cap, daily_used=wl.daily_used_usdt if wl.daily_used_usdt is not None else cap - wleft,
                     daily_left=wleft, daily_src="wallet")
        return d

    @staticmethod
    def _balance(ctx: LiveContext, asset: str) -> float:
        bal = {k.upper(): v for k, v in (ctx.balances or {}).items()}
        return float(bal.get(asset.upper(), 0.0))

    def _live_quote_checks(self, signal: Signal, ctx: LiveContext, notional: float, checks: list[RiskCheck]) -> None:
        lp = self._config.live
        q = ctx.quote
        if q is None:
            checks.append(RiskCheck(name="live_quote", passed=not lp.require_quote, detail=(
                "No quote supplied - a quote is required for LIVE BUYs" if lp.require_quote
                else "No quote supplied - quote guards run when the loop re-checks with the quote before executing")))
            return
        now = self._now(ctx)
        problems = []
        if q.symbol != signal.symbol or q.side != OrderSide.BUY:
            problems.append(f"quote is for {q.side.value} {q.symbol}, not BUY {signal.symbol}")
        age = (now - as_utc(q.created_at)).total_seconds()
        if q.is_expired(now):
            problems.append(f"quote expired at {as_utc(q.expires_at):%H:%M:%S} UTC")
        elif age > lp.max_quote_age_seconds:
            problems.append(f"quote is {age:.0f}s old (max {lp.max_quote_age_seconds:.0f}s)")
        checks.append(RiskCheck(name="live_quote", passed=not problems, detail=(
            "; ".join(problems).capitalize() if problems else f"Quote {q.quote_id[:8]} valid, {max(age, 0):.0f}s old")))

        ref = ctx.reference_price or signal.price or (ctx.bar.close if ctx.bar else None)
        ex_impact = self._exchange_impact(ctx)
        tol = q.slippage_bps / 100.0
        if ex_impact is not None:
            impact = abs(ex_impact)
            ok = impact <= lp.max_slippage_pct and tol <= lp.max_slippage_pct
            checks.append(RiskCheck(name="live_slippage", passed=ok, detail=(
                f"Exchange-reported price impact {impact:.2f}%, slippage tolerance {tol:.2f}%; max {lp.max_slippage_pct}%"
                + ("" if ok else " - too much slippage"))))
        elif ref is None:
            checks.append(RiskCheck(name="live_slippage", passed=False,
                                    detail="No reference price to measure the quote's price impact against"))
        else:
            impact = abs(q.price - ref) / ref * 100.0
            ok = impact <= lp.max_slippage_pct and tol <= lp.max_slippage_pct
            checks.append(RiskCheck(name="live_slippage", passed=ok, detail=(
                f"Quote price {q.price:.4f} vs reference {ref:.4f}: impact {impact:.2f}%, slippage tolerance {tol:.2f}%; "
                f"max {lp.max_slippage_pct}%" + ("" if ok else " - too much slippage"))))

        per = self._effective_caps(ctx)
        lim = min(notional, per["per_order"]) * (1 + 1e-9) + 1e-9
        ok = q.notional <= lim
        checks.append(RiskCheck(name="live_quote_notional", passed=ok, detail=(
            f"Quote notional {_fmt(q.notional)} USDT vs approved size {_fmt(notional)} USDT "
            f"(per-order cap {_fmt(per['per_order'])}, set by {per['per_order_src']})"
            + ("" if ok else " - re-quote for the approved size"))))

    def _check_buy(self, signal: Signal, portfolio: Portfolio, ctl: ControlState, ctx: LiveContext) -> RiskVerdict:
        cfg = self._config
        prules, erules, activation = cfg.rules_for(signal.symbol)
        checks: list[RiskCheck] = [RiskCheck(name="signal_action", passed=True, detail=f"BUY {signal.symbol} proposed by {signal.strategy}")]
        checks.append(self._mode_check(ctl))
        live = self._is_live(ctl, portfolio)
        lp = cfg.live
        checks.append(RiskCheck(name="emergency_stop", passed=not ctl.emergency_stopped,
                                detail="Emergency stop is active - all new entries are blocked" if ctl.emergency_stopped
                                else "No emergency stop"))
        checks.append(RiskCheck(name="paused", passed=not ctl.paused,
                                detail="Agent is paused - new entries are blocked" if ctl.paused else "Agent is running"))

        n_open = len(portfolio.positions)
        checks.append(RiskCheck(
            name="max_open_positions", passed=n_open < prules.max_open_positions,
            detail=f"{n_open} open position(s), limit {prules.max_open_positions}"
            + ("" if n_open < prules.max_open_positions else " - limit reached")))

        bse = portfolio.bars_since_exit.get(signal.symbol)
        if bse is None:
            checks.append(RiskCheck(name="cooldown", passed=True, detail=f"No recent exit on {signal.symbol}"))
        else:
            ok = bse > prules.cooldown_bars
            checks.append(RiskCheck(name="cooldown", passed=ok, detail=(
                f"{bse} bar(s) since last exit on {signal.symbol}, cooldown {prules.cooldown_bars}"
                + ("" if ok else f" - wait {prules.cooldown_bars - bse + 1} more bar(s)"))))

        if cfg.daily_loss_limit_pct is None:
            checks.append(RiskCheck(name="daily_loss_limit", passed=True, detail="Daily loss limit off"))
        else:
            limit = portfolio.starting_capital * cfg.daily_loss_limit_pct / 100.0
            ok = portfolio.daily_pnl > -limit
            checks.append(RiskCheck(name="daily_loss_limit", passed=ok, detail=(
                f"Today's PnL {_fmt(portfolio.daily_pnl)} USDT vs limit -{_fmt(limit)} USDT ({cfg.daily_loss_limit_pct}% of capital)"
                + ("" if ok else " - daily loss limit hit, no new entries today"))))

        if cfg.max_trades_per_day is None:
            checks.append(RiskCheck(name="max_trades_per_day", passed=True, detail="Daily trade limit off"))
        else:
            ok = portfolio.trades_today < cfg.max_trades_per_day
            checks.append(RiskCheck(name="max_trades_per_day", passed=ok,
                                    detail=f"{portfolio.trades_today} trade(s) today, limit {cfg.max_trades_per_day}"))

        if live:
            self._live_buy_gates(signal, portfolio, ctx, checks)

        # ---- sizing (all reductions recorded; failure only if the order ends up too small)
        equity = self._equity(portfolio)
        gates_ok = all(c.passed for c in checks)
        notional = 0.0
        if equity <= 0:
            checks.append(RiskCheck(name="position_size", passed=False, detail="Portfolio equity is zero - cannot size an order"))
        else:
            base_pct = prules.position_size_pct
            want_pct = base_pct
            src = f"rule {base_pct}% of equity"
            if signal.proposed_notional > 0:
                want_pct = signal.proposed_notional / equity * 100.0
                src = f"signal asked {_fmt(signal.proposed_notional)} USDT ({want_pct:.2f}%)"
            elif signal.proposed_size_pct > 0:
                want_pct = signal.proposed_size_pct
                src = f"signal asked {want_pct}%"
            pct = min(want_pct, base_pct)
            notional = equity * pct / 100.0
            checks.append(RiskCheck(name="position_size", passed=True, detail=(
                f"{src}; size {pct:.2f}% of equity {_fmt(equity)} = {_fmt(notional)} USDT"
                + (f" (capped at max {base_pct}%)" if want_pct > base_pct else ""))))

            def cap(name: str, limit: float, what: str) -> None:
                nonlocal notional
                if notional > limit:
                    checks.append(RiskCheck(name=name, passed=True,
                                            detail=f"{what}: reduced from {_fmt(notional)} to {_fmt(max(limit, 0))} USDT"))
                    notional = max(limit, 0.0)
                else:
                    checks.append(RiskCheck(name=name, passed=True, detail=f"{what}: {_fmt(notional)} USDT fits"))

            cap("cash_available", portfolio.cash, f"Cash available {_fmt(portfolio.cash)} USDT")
            token_exp = sum(self._position_value(p) for p in portfolio.positions if p.symbol == signal.symbol)
            token_lim = equity * cfg.max_token_exposure_pct / 100.0
            cap("max_token_exposure", token_lim - token_exp,
                f"{signal.symbol} exposure {_fmt(token_exp)} USDT, limit {cfg.max_token_exposure_pct}% = {_fmt(token_lim)} USDT")
            total_exp = sum(self._position_value(p) for p in portfolio.positions)
            total_lim = equity * cfg.max_total_exposure_pct / 100.0
            cap("max_total_exposure", total_lim - total_exp,
                f"Total exposure {_fmt(total_exp)} USDT, limit {cfg.max_total_exposure_pct}% = {_fmt(total_lim)} USDT")

            if prules.max_loss_per_trade_pct:
                max_loss = portfolio.starting_capital * prules.max_loss_per_trade_pct / 100.0
                sl = erules.stop_loss_pct / 100.0
                cap("max_loss_per_trade", max_loss / sl,
                    f"Max loss per trade {prules.max_loss_per_trade_pct}% = {_fmt(max_loss)} USDT at a {erules.stop_loss_pct}% stop")
            else:
                checks.append(RiskCheck(name="max_loss_per_trade", passed=True, detail="Max loss per trade off"))

            if live:
                cap("live_order_cap", lp.max_order_notional_usdt, f"LIVE per-order cap {_fmt(lp.max_order_notional_usdt)} USDT")
                cap("live_max_exposure", lp.max_open_exposure_usdt - total_exp,
                    f"LIVE open exposure {_fmt(total_exp)} USDT, cap {_fmt(lp.max_open_exposure_usdt)} USDT")
                used = self.daily_live_notional_used(ctx)
                cap("live_daily_notional", lp.max_daily_notional_usdt - used,
                    f"LIVE bought today {_fmt(used)} USDT, daily cap {_fmt(lp.max_daily_notional_usdt)} USDT")
                wl = ctx.wallet_limits
                if wl is not None:
                    if wl.per_tx_limit_usdt is not None:
                        cap("wallet_per_tx_cap", wl.per_tx_limit_usdt,
                            f"Wallet per-transaction limit {_fmt(wl.per_tx_limit_usdt)} USDT")
                    wleft = wl.daily_remaining_usdt()
                    if wleft is not None:
                        cap("wallet_daily_limit", wleft,
                            f"Wallet daily limit {self._opt(wl.daily_limit_usdt)}, used {self._opt(wl.daily_used_usdt)}, "
                            f"left {_fmt(wleft)} USDT")
                        if lp.reserve_wallet_quota_for_exits:
                            cap("wallet_exit_reserve", (wleft - total_exp) / 2.0, (
                                f"Wallet daily quota left {_fmt(wleft)} USDT also pays for SELLs: keeping "
                                f"{_fmt(total_exp)} USDT of it to sell open positions plus the same amount as this buy to "
                                f"sell it later, so the buy is at most (left - open exposure) / 2"
                                f" = {_fmt(max((wleft - total_exp) / 2.0, 0.0))} USDT"))
                if ctx.balances is not None:
                    stable = self._balance(ctx, lp.quote_asset)
                    cap("live_wallet_balance", stable - lp.fee_buffer_usdt,
                        f"Wallet {lp.quote_asset} {_fmt(stable)} minus fee buffer {_fmt(lp.fee_buffer_usdt)}")

            tmin = ctx.token_status.min_order_usdt if (live and ctx.token_status) else None
            min_n = max(cfg.min_order_notional, tmin or 0.0)
            ok = notional >= max(min_n, 1e-9)
            token_bound = tmin is not None and tmin > cfg.min_order_notional
            checks.append(RiskCheck(name="min_order_notional", passed=ok, detail=(
                f"Order {_fmt(notional)} USDT, minimum {_fmt(min_n)} USDT"
                + (f" ({signal.symbol} token minimum)" if token_bound else "")
                + ("" if ok else (" - below token minimum order" if token_bound else " - too small")))))
            if live:
                self._live_quote_checks(signal, ctx, notional, checks)
                if ctx.quote is not None and ctx.balances is not None:
                    stable = self._balance(ctx, lp.quote_asset)
                    ok = ctx.quote.notional + ctx.quote.fee <= stable - lp.fee_buffer_usdt + 1e-9
                    checks.append(RiskCheck(name="live_wallet_quote", passed=ok, detail=(
                        f"Quote spends {_fmt(ctx.quote.notional + ctx.quote.fee)} {lp.quote_asset} incl. fee; wallet "
                        f"{_fmt(stable)} minus buffer {_fmt(lp.fee_buffer_usdt)}" + ("" if ok else " - insufficient balance"))))

        approved = gates_ok and all(c.passed for c in checks)
        needs_conf = ctl.confirmation_mode or activation == ActivationMode.CONFIRMATION
        if approved:
            checks.append(RiskCheck(name="confirmation", passed=True, detail=(
                "Confirmation mode: waiting for the user to approve this order" if needs_conf
                else "Auto-execute: no confirmation needed")))
        if not approved:
            notional = 0.0
        size_pct = min(100.0, notional / equity * 100.0) if equity > 0 else 0.0
        return self._verdict(signal, approved, checks, size_pct, notional, approved and needs_conf)

    def _check_sell(self, signal: Signal, portfolio: Portfolio, ctl: ControlState) -> RiskVerdict:
        cfg = self._config
        risk_exit = is_risk_exit(signal)
        checks = [RiskCheck(name="signal_action", passed=True, detail=(
            f"SELL {signal.symbol}" + (" (protective exit from risk layer)" if risk_exit else f" proposed by {signal.strategy}")))]
        checks.append(self._mode_check(ctl, Action.SELL))
        checks.append(RiskCheck(name="emergency_stop", passed=True, detail=(
            "Emergency stop active - closing positions is allowed" if ctl.emergency_stopped else "No emergency stop")))
        if ctl.paused and not (cfg.allow_exits_when_paused or ctl.emergency_stopped):
            checks.append(RiskCheck(name="paused", passed=False, detail="Agent is paused - exits are blocked by config"))
        else:
            checks.append(RiskCheck(name="paused", passed=True, detail=(
                "Agent is paused - protective exits still allowed" if ctl.paused else "Agent is running")))
        held = [p for p in portfolio.positions if p.symbol == signal.symbol
                and (signal.position_id is None or p.id == signal.position_id)]
        value = sum(self._position_value(p, signal.price) for p in held)
        target = f"position {signal.position_id} ({signal.symbol})" if signal.position_id else signal.symbol
        checks.append(RiskCheck(name="open_position", passed=bool(held), detail=(
            f"Holding {sum(p.quantity for p in held):g} {signal.symbol} worth {_fmt(value)} USDT in {target}" if held
            else f"No open {target} - nothing to sell")))
        notional = 0.0
        if held:
            notional = min(signal.proposed_notional, value) if signal.proposed_notional > 0 else value
            checks.append(RiskCheck(name="sell_size", passed=True, detail=(
                f"Sell {_fmt(notional)} USDT of {_fmt(value)} USDT" + (" (full position)" if notional >= value else ""))))
        approved = all(c.passed for c in checks)
        needs_conf = False
        if approved:
            _, _, activation = cfg.rules_for(signal.symbol)
            wants_conf = ctl.confirmation_mode or activation == ActivationMode.CONFIRMATION
            bypass = (risk_exit and cfg.risk_exits_bypass_confirmation) or ctl.emergency_stopped
            needs_conf = wants_conf and not bypass
            checks.append(RiskCheck(name="confirmation", passed=True, detail=(
                "Confirmation mode: waiting for the user to approve this sell" if needs_conf
                else "Protective exit - executes without confirmation" if wants_conf
                else "Auto-execute: no confirmation needed")))
        else:
            notional = 0.0
        equity = self._equity(portfolio)
        size_pct = min(100.0, notional / equity * 100.0) if equity > 0 else 0.0
        return self._verdict(signal, approved, checks, size_pct, notional, needs_conf)

    def _verdict(self, signal: Signal, approved: bool, checks: list[RiskCheck], size_pct: float, notional: float,
                 requires_confirmation: bool) -> RiskVerdict:
        if approved:
            verb = signal.action.value
            reasons = [f"Approved {verb} {signal.symbol} for {_fmt(notional)} USDT ({size_pct:.2f}% of equity)"
                       + (" - awaiting user confirmation" if requires_confirmation else "")]
            reasons += [c.detail for c in checks if "reduced" in c.detail]
        else:
            reasons = [f"Blocked ({c.name}): {c.detail}" for c in checks if not c.passed]
        return RiskVerdict(signal_id=signal.id, approved=approved, reasons=reasons, checks=checks,
                           adjusted_size_pct=round(size_pct, 6), adjusted_notional=round(notional, 8),
                           requires_confirmation=requires_confirmation, timestamp=self._clock())

    # ------------------------------------------------------------------ exits
    def check_exits(
        self, portfolio: Portfolio, prices: dict[str, float], bars: Optional[dict[str, Candle]] = None
    ) -> list[Signal]:
        """SL / TP / trailing / max-loss / emergency enforcement -> SELL signals (one per position).

        ``bars`` (symbol -> latest closed bar) enables the intrabar rules (low/high hit tests, SL wins if both hit,
        fill at the level or at the open on a gap). Symbols without a bar fall back to ``prices`` (degenerate bar,
        open=high=low=close=price), then to ``Position.current_price``. The portfolio is never mutated."""
        bars = bars or {}
        out: list[Signal] = []
        with self._lock:
            if self._is_live(None, portfolio):
                self._enforce_daily_loss_cap(portfolio, None)
            ctl = self._control
            cfg = self._config
        for p in portfolio.positions:
            candle = bars.get(p.symbol)
            if candle is not None:
                bar, ts = Bar.from_candle(candle), candle.timestamp
            else:
                px = prices.get(p.symbol) or p.current_price
                bar, ts = (Bar.from_price(px) if px and px > 0 else None), self._clock()
            if ctl.emergency_stopped:
                price = bar.close if bar else (p.current_price or p.avg_entry_price)
                pnl = (price - p.avg_entry_price) / p.avg_entry_price * 100.0
                out.append(self._exit_signal(p, ExitReason.EMERGENCY_STOP, price, ts,
                                             f"EMERGENCY_STOP: closing {p.symbol} at market (~{price:.4f}, PnL {pnl:+.2f}%)"
                                             + (f" - {ctl.reason}" if ctl.reason else "")))
                continue
            if bar is None:
                continue
            prules, erules, _ = cfg.rules_for(p.symbol)
            levels = compute_levels(p, erules, self._hwm_for(p),
                                    max_loss_level(p, prules.max_loss_per_trade_pct, portfolio.starting_capital))
            decision, _ = evaluate_bar(levels, bar)
            if decision is not None:
                out.append(self._exit_signal(p, decision.reason, decision.fill_price, ts, decision.detail))
        return out

    def check_exits_candles(self, portfolio: Portfolio, candles: dict[str, Candle]) -> list[Signal]:
        """Deprecated alias of ``check_exits(portfolio, {}, bars=candles)``."""
        return self.check_exits(portfolio, {}, bars=dict(candles))

    @staticmethod
    def _exit_signal(p: Position, reason: ExitReason, price: float, ts: datetime, detail: str) -> Signal:
        source = AgentMode.MANUAL if p.strategy.upper().startswith("MANUAL") else AgentMode.AUTO
        return Signal(symbol=p.symbol, action=Action.SELL, source=source, strategy=p.strategy, reason=detail,
                      conditions_met=[reason.value], price=price, exit_reason=reason, position_id=p.id,
                      proposed_notional=round(p.quantity * price, 8), timestamp=ts)


__all__ = ["DefaultRiskManager", "RISK_EXIT_REASONS", "is_risk_exit"]
