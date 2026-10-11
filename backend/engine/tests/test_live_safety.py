"""Live / mainnet safety layer: DefaultStrategyEngine(live_safe=True, clock=...)."""

from datetime import datetime, timedelta, timezone

import pytest

from backend.contracts import (
    Action,
    AutoConfig,
    Candle,
    ExitReason,
    MarketHoursFilter,
    Portfolio,
    Position,
    Regime,
    TraderStyle,
)
from backend.engine import DefaultStrategyEngine
from backend.engine.live import (
    Liquidity,
    LiveSafetyConfig,
    cap_size_pct,
    entry_blockers,
    liquidity,
    thin_liquidity_factor,
)
from backend.mocks import generate_candles

from .helpers import entry_scenario, sma_entry_strategy

H = timedelta(hours=1)


def shifted(cs: list[Candle], last_at: datetime) -> list[Candle]:
    d = last_at - cs[-1].timestamp
    return [c.model_copy(update={"timestamp": c.timestamp + d}) for c in cs]


WED_1400 = datetime(2026, 1, 7, 14, tzinfo=timezone.utc)  # inside the 13:00-20:00 UTC weekday session
ENTRY = shifted(entry_scenario([], tail=0), WED_1400)  # manual BUY on the last bar (close > SMA(2))
UPTREND = shifted(generate_candles(Regime.STRONG_UPTREND, n=200, seed=42)[:61], WED_1400)  # auto BUY (BALANCED)


def pf(positions=(), equity=1000.0):
    return Portfolio(starting_capital=1000, cash=equity, equity=equity, positions=list(positions))


def live(now: datetime, **cfg) -> DefaultStrategyEngine:
    return DefaultStrategyEngine(
        live_safe=True, clock=lambda: now, live_config=LiveSafetyConfig(**cfg) if cfg else None
    )


FRESH = WED_1400 + 30 * timedelta(minutes=1)  # bar opened 14:00, now 14:30


def test_default_engine_unchanged_and_live_allows_fresh_in_session():
    s = sma_entry_strategy()  # position_size_pct 50
    base = DefaultStrategyEngine().evaluate_manual(s, ENTRY, pf())
    assert base.action == Action.BUY and base.proposed_size_pct == 50 and "live-safe" not in base.reason
    sig = live(FRESH).evaluate_manual(s, ENTRY, pf())
    assert sig.action == Action.BUY
    assert sig.proposed_size_pct == 2.0 and sig.proposed_notional == pytest.approx(20.0)  # 1000 * 2%
    assert "live-safe size 50% x0.25 capped at 2% -> 2%" in sig.reason


def test_size_caps_small_and_consistent_with_risk_usdt_caps():
    s = sma_entry_strategy()
    s.risk.position_size_pct = 15
    assert live(FRESH).evaluate_manual(s, ENTRY, pf()).proposed_size_pct == 2.0
    s.risk.position_size_pct = 5
    sig = live(FRESH).evaluate_manual(s, ENTRY, pf())
    assert sig.proposed_size_pct == 1.25 and sig.proposed_notional == pytest.approx(12.5)
    cfg = LiveSafetyConfig()
    assert cap_size_pct(10, cfg) == 2.0 and cap_size_pct(5, cfg) == 1.25 and cap_size_pct(0.4, cfg) == 0.1
    # On a 1000 USDT account a live-safe BUY never exceeds 20 USDT, below Bot 3's 25 USDT per-order cap.
    assert 1000 * cap_size_pct(100, cfg) / 100 <= 25


def test_auto_buy_is_capped():
    cfg = AutoConfig(symbol="NVDAon", capital=1000, style=TraderStyle.BALANCED)
    base = DefaultStrategyEngine().evaluate_auto(cfg, UPTREND, pf())
    assert base.action == Action.BUY and base.proposed_size_pct == pytest.approx(10 * 1.0, rel=0.6)
    sig = live(FRESH).evaluate_auto(cfg, UPTREND, pf())
    assert sig.action == Action.BUY and sig.proposed_size_pct <= 2.0
    assert sig.proposed_notional == pytest.approx(1000 * sig.proposed_size_pct / 100) and "live-safe" in sig.reason


@pytest.mark.parametrize(
    "now, blocker",
    [
        (WED_1400 + 3 * H, "live_stale_data"),  # 3 bars old on 1h
        (WED_1400 - 3 * H, "live_future_data"),  # clock behind / bad data
        (WED_1400 + 2 * H + timedelta(minutes=1), "live_stale_data"),  # just over the 2-bar limit
    ],
)
def test_stale_and_future_data_block_entries(now, blocker):
    s = sma_entry_strategy()
    sig = live(now).evaluate_manual(s, ENTRY, pf())
    assert sig.action == Action.STAY_OUT and blocker in sig.conditions_failed
    assert sig.reason.startswith("Live safety blocked entry")
    auto = live(now).evaluate_auto(AutoConfig(symbol="NVDAon", capital=1000, style=TraderStyle.BALANCED), UPTREND, pf())
    assert auto.action == Action.STAY_OUT and blocker in auto.conditions_failed


def test_two_bars_old_is_still_fresh():
    assert live(WED_1400 + 2 * H).evaluate_manual(sma_entry_strategy(), ENTRY, pf()).action == Action.BUY


def test_market_hours_off_by_default_24_7():
    s = sma_entry_strategy()
    assert LiveSafetyConfig().market_hours.enabled is False
    for last in (datetime(2026, 1, 7, 3, tzinfo=timezone.utc), datetime(2026, 1, 10, 15, tzinfo=timezone.utc)):
        cs = shifted(ENTRY, last)  # 03:00 Wednesday, Saturday afternoon
        assert live(last + H / 2).evaluate_manual(s, cs, pf()).action == Action.BUY


def test_market_hours_opt_in_for_stock_tokens():
    s = sma_entry_strategy()
    on = MarketHoursFilter(enabled=True)
    early = shifted(ENTRY, datetime(2026, 1, 7, 12, tzinfo=timezone.utc))
    sig = live(early[-1].timestamp + 10 * timedelta(minutes=1), market_hours=on).evaluate_manual(s, early, pf())
    assert sig.action == Action.STAY_OUT and "live_market_hours" in sig.conditions_failed
    # bar inside the session, but 'now' after the close (bar still fresh: 19:00 bar, now 20:30)
    late = shifted(ENTRY, datetime(2026, 1, 7, 19, tzinfo=timezone.utc))
    sig = live(datetime(2026, 1, 7, 20, 30, tzinfo=timezone.utc), market_hours=on).evaluate_manual(s, late, pf())
    assert sig.conditions_failed == ["live_market_hours_now"]
    sat = shifted(ENTRY, datetime(2026, 1, 10, 15, tzinfo=timezone.utc))
    sig = live(sat[-1].timestamp + H / 2, market_hours=on).evaluate_manual(s, sat, pf())
    assert {"live_market_hours", "live_market_hours_now"} <= set(sig.conditions_failed)
    assert live(FRESH, market_hours=on).evaluate_manual(s, ENTRY, pf()).action == Action.BUY  # Wed 14:00
    # A non-stock token (no Ondo/xStock/bStock suffix) is never session-bound.
    s.symbol = "ETH"  # note: "BNB"/"WBNB" end in 'B' and are (conservatively) treated as bStocks by the suffix rule
    assert live(early[-1].timestamp + H / 6, market_hours=on).evaluate_manual(s, early, pf()).action == Action.BUY


def with_volumes(cs: list[Candle], vols: list[float]) -> list[Candle]:
    """Overwrite the volumes of the last len(vols) bars."""
    n = len(vols)
    return cs[:-n] + [c.model_copy(update={"volume": v}) for c, v in zip(cs[-n:], vols, strict=True)]


def test_thin_liquidity_blocks_entry():
    cs = with_volumes(ENTRY, [0.0] * 16 + [5.0, 0.0, 0.0, 5.0])  # 2 of 20 bars traded (overnight)
    sig = live(FRESH).evaluate_manual(sma_entry_strategy(), cs, pf())
    assert sig.action == Action.STAY_OUT and sig.conditions_failed == ["live_thin_liquidity"]
    assert "10% of last 20 bars traded" in sig.reason
    assert DefaultStrategyEngine().evaluate_manual(sma_entry_strategy(), cs, pf()).action == Action.BUY  # sim unchanged


def test_thin_but_tradeable_liquidity_halves_size():
    s = sma_entry_strategy()  # 50% -> live-safe 2%
    half = with_volumes(ENTRY, [0.0, 5.0] * 10)  # 50% of bars traded
    sig = live(FRESH).evaluate_manual(s, half, pf())
    assert sig.action == Action.BUY and sig.proposed_size_pct == 1.0 and "thin liquidity" in sig.reason
    assert sig.proposed_notional == pytest.approx(10.0)
    weak_last = with_volumes(ENTRY, [1000.0] * 19 + [100.0])  # all traded, last bar only 0.1x the average
    assert live(FRESH).evaluate_manual(s, weak_last, pf()).proposed_size_pct == 1.0
    assert live(FRESH).evaluate_manual(s, ENTRY, pf()).proposed_size_pct == 2.0  # normal liquidity
    assert live(FRESH, thin_size_factor=1.0).evaluate_manual(s, half, pf()).proposed_size_pct == 2.0


def test_liquidity_unit():
    assert liquidity([], 20) == Liquidity(0.0, 0.0, 0)
    liq = liquidity(with_volumes(ENTRY, [0.0, 2.0, 0.0, 6.0]), 4)
    assert liq.active_fraction == 0.5 and liq.relative_volume == pytest.approx(1.5)
    assert thin_liquidity_factor(ENTRY, LiveSafetyConfig()) == (1.0, None)


def test_no_volume_on_last_bar_blocks():
    cs = ENTRY[:-1] + [ENTRY[-1].model_copy(update={"volume": 0.0})]
    sig = live(FRESH).evaluate_manual(sma_entry_strategy(), cs, pf())
    assert sig.action == Action.STAY_OUT and sig.conditions_failed == ["live_no_volume"]
    assert live(FRESH, require_volume=False).evaluate_manual(sma_entry_strategy(), cs, pf()).action == Action.BUY


def test_sell_is_never_blocked():
    s = sma_entry_strategy()
    pos = Position(symbol="NVDAon", quantity=2, avg_entry_price=101, strategy="MANUAL:test")
    cs = shifted(entry_scenario([(101, 102, 90, 91)], tail=0), datetime(2026, 1, 10, 3, tzinfo=timezone.utc))
    cs[-1] = cs[-1].model_copy(update={"volume": 0.0})
    eng = DefaultStrategyEngine(live_safe=True, manual_price_exits=True, clock=lambda: cs[-1].timestamp + 50 * H)
    # stale (50 bars), Saturday night, no volume: entries would be blocked, the stop-loss exit is not
    sig = eng.evaluate_manual(s, cs, pf([pos]))
    assert sig.action == Action.SELL and sig.exit_reason == ExitReason.STOP_LOSS and sig.position_id == pos.id
    assert sig.proposed_size_pct == 100


def test_warming_up_indicator_blocks_live_entry_only():
    s = sma_entry_strategy()
    s.entry.price_above_ema.enabled = True
    s.entry.price_above_ema.period = 500  # never ready on 61 bars -> skipped
    assert DefaultStrategyEngine().evaluate_manual(s, ENTRY, pf()).action == Action.BUY  # sim: skipped != false
    sig = live(FRESH).evaluate_manual(s, ENTRY, pf())
    assert sig.action == Action.STAY_OUT and "warming up" in sig.reason
    assert live(FRESH, require_all_conditions_ready=False).evaluate_manual(s, ENTRY, pf()).action == Action.BUY


def test_insufficient_data_stays_out_in_live():
    sig = live(FRESH).evaluate_manual(sma_entry_strategy(), ENTRY[-30:], pf())
    assert sig.action == Action.STAY_OUT and sig.conditions_failed == ["insufficient_data"]


def test_entry_blockers_unit_and_config_validation():
    cfg = LiveSafetyConfig()
    assert entry_blockers("NVDAon", [], 3600, FRESH, cfg) == [("live_no_data", "no valid candles")]
    assert entry_blockers("NVDAon", ENTRY, 3600, FRESH, cfg) == []
    naive_now = FRESH.replace(tzinfo=None)  # naive datetimes are treated as UTC
    assert entry_blockers("NVDAon", ENTRY, 3600, naive_now, cfg) == []
    assert [n for n, _ in entry_blockers("NVDAon", ENTRY, 300, FRESH, cfg)] == ["live_stale_data"]  # 5m bars
    bads = (
        {"size_factor": 0},
        {"size_factor": 1.5},
        {"max_size_pct": 0},
        {"max_stale_bars": 0},
        {"liquidity_lookback": 0},
        {"min_active_fraction": 0.9},
        {"thin_size_factor": 0},
    )
    for bad in bads:
        with pytest.raises(ValueError):
            LiveSafetyConfig(**bad)
