"""Live-safety behaviour on REAL Binance samples (bStock NVDAB, Ondo NVDAon, xStock TSLAx; Sun 11 Oct 2026)."""

from datetime import timedelta

import pytest

from backend.contracts import Action, AutoConfig, ManualStrategy, Portfolio, Regime, TraderStyle
from backend.engine import DefaultStrategyEngine
from backend.engine.data import (
    FAMILY_BSTOCK,
    FAMILY_ONDO,
    FAMILY_OTHER,
    FAMILY_XSTOCK,
    infer_bar_seconds,
    sanitize_candles,
    token_family,
)
from backend.engine.live import LiveSafetyConfig, entry_blockers, live_size, normalize_market_context
from backend.engine.regime import detect_regime

from .binance_samples import NOW, extended, kline_candles, load_json

H = timedelta(hours=1)


def pf(equity=1000.0):
    return Portfolio(starting_capital=equity, cash=equity, equity=equity)


def live(now=NOW, market_context=None, **cfg) -> DefaultStrategyEngine:
    return DefaultStrategyEngine(
        live_safe=True,
        clock=lambda: now,
        live_config=LiveSafetyConfig(**cfg) if cfg else None,
        market_context=market_context,
    )


def always_in(symbol: str, volume: bool = True) -> ManualStrategy:
    """RSI(14) < 100 (always true once warm) + volume_above_avg + the min-volume filter."""
    s = ManualStrategy(name="real", symbol=symbol)
    s.entry.rsi_below.threshold = 100
    s.entry.price_above_sma.enabled = False
    s.entry.volume_above_avg.enabled = volume
    s.filters.min_volume.enabled = volume
    s.risk.position_size_pct = 10
    return s


# ---------------------------------------------------------------- the samples themselves
def test_token_family():
    assert [token_family(s) for s in ("NVDAB", "NVDAon", "TSLAx", "ETH", "")] == [
        FAMILY_BSTOCK,
        FAMILY_ONDO,
        FAMILY_XSTOCK,
        FAMILY_OTHER,
        FAMILY_OTHER,
    ]


def test_real_samples_parse_and_shape():
    b, o, x = (sanitize_candles(kline_candles(s))[1] for s in ("NVDAB", "NVDAon", "TSLAx"))
    assert (b.kept, b.gaps, b.zero_volume, b.bar_seconds) == (48, 0, 0, 3600)  # bStock: 24/7 with volume
    assert (o.kept, o.gaps, o.zero_volume, o.bar_seconds) == (48, 0, 48, 3600)  # Ondo: every bar volume 0
    assert x.kept == 48 and x.gaps > 30  # xStock: bars weeks apart
    assert infer_bar_seconds(kline_candles("NVDAon")) == 3600


def test_real_samples_regime_never_crashes_and_ondo_is_sideways():
    for s in ("NVDAB", "NVDAon", "TSLAx"):
        ra = detect_regime(kline_candles(s))  # 48 bars < 61: insufficient -> low-confidence SIDEWAYS
        assert ra.regime == Regime.SIDEWAYS and ra.confidence <= 0.2 and ra.features["insufficient_data"] == 1
    ra = detect_regime(extended("NVDAon"))
    assert ra.regime == Regime.SIDEWAYS and ra.features["volatility_pct"] < 0.2  # oracle-like off-hours prices


def test_entry_blockers_per_family_on_real_bars():
    cfg = LiveSafetyConfig()
    assert entry_blockers("NVDAB", kline_candles("NVDAB"), 3600, NOW, cfg) == []
    # THE BUG: Ondo's zero-volume bars used to block every entry (live_no_volume + live_thin_liquidity).
    assert entry_blockers("NVDAon", kline_candles("NVDAon"), 3600, NOW, cfg) == []
    # ... but stale data still applies to Ondo
    assert [n for n, _ in entry_blockers("NVDAon", kline_candles("NVDAon"), 3600, NOW + 5 * H, cfg)] == [
        "live_stale_data"
    ]
    # Same zero-volume bars on a family with volume rules -> still blocked
    names = [n for n, _ in entry_blockers("NVDAB", kline_candles("NVDAon"), 3600, NOW, cfg)]
    assert names == ["live_no_volume", "live_thin_liquidity"]
    x = [n for n, _ in entry_blockers("TSLAx", kline_candles("TSLAx"), 3600, NOW, cfg)]
    assert x[0] == "live_illiquid_family" and "live_stale_data" in x
    relaxed = LiveSafetyConfig(blocked_families=frozenset())
    xs = kline_candles("TSLAx")
    assert entry_blockers("TSLAx", xs, 3600, xs[-1].timestamp + H, relaxed) == []  # configurable


# ---------------------------------------------------------------- manual evaluator, real Ondo / bStock data
def test_ondo_volume_conditions_not_applicable_in_live():
    cs = extended("NVDAon")
    sig = live().evaluate_manual(always_in("NVDAon"), cs, pf())
    assert sig.action == Action.BUY
    assert sig.conditions_met == ["rsi_below"] and "volume_above_avg" not in sig.conditions_failed
    assert "volume condition n/a" in sig.reason
    # 10% x0.25 = 2.5% capped at 2% = 20 USDT on 1000: exactly Ondo's 20 USD minimum
    assert sig.proposed_notional == pytest.approx(20.0)
    # The same zero-volume data on a bStock is refused, and the SIM engine (no live_safe) keeps the strict filter.
    b = live().evaluate_manual(always_in("NVDAB"), cs, pf())
    assert b.action == Action.STAY_OUT and b.conditions_failed == ["live_no_volume", "live_thin_liquidity"]
    sim = DefaultStrategyEngine().evaluate_manual(always_in("NVDAon"), cs, pf())
    assert sim.action == Action.STAY_OUT and sim.conditions_failed == ["min_volume"]


def test_ondo_only_volume_condition_means_no_entry():
    s = always_in("NVDAon")
    s.entry.rsi_below.enabled = False  # volume is the only condition and it is n/a -> nothing to act on
    sig = live().evaluate_manual(s, extended("NVDAon"), pf())
    assert sig.action == Action.STAY_OUT and "no entry conditions" in sig.reason


def test_bstock_real_data_buys_with_volume_rules():
    s = always_in("NVDAB", volume=False)
    sig = live().evaluate_manual(s, extended("NVDAB"), pf())
    assert sig.action == Action.BUY and sig.proposed_notional == pytest.approx(20.0)


def test_xstock_blocked_in_manual_and_auto():
    xs = kline_candles("TSLAx")
    now = xs[-1].timestamp + H / 2
    long = extended("TSLAx")
    sig = live(now).evaluate_manual(always_in("TSLAx", volume=False), long, pf())
    assert sig.action == Action.STAY_OUT and sig.conditions_failed[0] == "live_illiquid_family"


@pytest.mark.parametrize("style", list(TraderStyle))
def test_auto_on_real_samples(style):
    for sym in ("NVDAB", "NVDAon", "TSLAx"):
        for cs in (kline_candles(sym), extended(sym)):
            sig = live().evaluate_auto(AutoConfig(symbol=sym, capital=1000, style=style), cs, pf())
            assert sig.action in (Action.BUY, Action.STAY_OUT)
            if sig.action == Action.BUY:
                assert sym != "TSLAx" and 0 < sig.proposed_notional <= 25


# ---------------------------------------------------------------- min order floor + 25 USDT cap
@pytest.mark.parametrize(
    "sym, pct, equity, expected",
    [
        ("NVDAon", 10, 1000, 20.0),  # 2% of 1000 = 20 -> already at the floor
        ("NVDAon", 5, 1000, 20.0),  # 1.25% = 12.5 -> bumped to 20
        ("NVDAon", 10, 200, 20.0),  # 2% of 200 = 4 -> bumped to 20 (10% of equity; risk caps still apply)
        ("NVDAB", 5, 1000, 12.5),  # no floor for bStock
        ("NVDAB", 10, 10_000, 25.0),  # 2% of 10k = 200 -> capped at 25 USDT
        ("NVDAon", 10, 10_000, 25.0),
    ],
)
def test_min_order_floor_and_cap(sym, pct, equity, expected):
    s = always_in(sym, volume=False)
    s.risk.position_size_pct = pct
    sig = live().evaluate_manual(s, extended(sym), pf(equity))
    assert sig.action == Action.BUY and sig.proposed_notional == pytest.approx(expected)
    assert sig.proposed_size_pct == pytest.approx(expected / equity * 100)


def test_below_min_order_stays_out():
    s = always_in("NVDAon", volume=False)
    cs = extended("NVDAon")
    tiny = live().evaluate_manual(s, cs, pf(15))  # floor 20 > equity 15
    assert tiny.action == Action.STAY_OUT and tiny.conditions_failed == ["live_below_min_order"]
    small_cap = live(max_order_usdt=15).evaluate_manual(s, cs, pf(200))  # floor 20 > max order 15
    assert small_cap.conditions_failed == ["live_below_min_order"] and "max order 15" in small_cap.reason
    no_bump = live(bump_to_min_order=False).evaluate_manual(s, cs, pf(200))
    assert no_bump.conditions_failed == ["live_below_min_order"]
    custom = live(min_order_usdt={"ondo": 20.0, "bstock": 5.0})
    b = always_in("NVDAB", volume=False)
    b.risk.position_size_pct = 1  # 0.25% of 1000 = 2.5 -> bumped to the custom 5 USDT bStock floor
    assert custom.evaluate_manual(b, extended("NVDAB"), pf()).proposed_notional == pytest.approx(5.0)


def test_equity_hint_and_never_above_cap():
    cfg = LiveSafetyConfig(equity_hint_usdt=300)
    d = live_size("NVDAB", 100, 1000, extended("NVDAB"), cfg)
    assert d.notional == pytest.approx(6.0) and d.pct == pytest.approx(0.6)  # 2% of the 300 hint
    for sym in ("NVDAB", "NVDAon", "ETH"):
        for pct in (0.1, 1, 10, 50, 100):
            for eq in (25, 100, 1000, 1e6):
                d = live_size(sym, pct, eq, extended("NVDAB"), LiveSafetyConfig())
                assert d.notional <= 25.0 + 1e-9


# ---------------------------------------------------------------- market context
def test_market_context_real_status_payloads():
    eng = live()
    s = always_in("NVDAon", volume=False)
    cs = extended("NVDAon")
    assert eng.evaluate_manual(s, cs, pf()).action == Action.BUY  # no context: candles only
    # Real Binance status (offhours) -> Bot 3's rule: off-hours refuses new buys
    eng.set_market_context("NVDAon", load_json("status_NVDAon.json")["data"])
    assert eng.market_context("NVDAon")["status"] == "offhours"
    sig = eng.evaluate_manual(s, cs, pf())
    assert sig.action == Action.STAY_OUT and sig.conditions_failed == ["live_token_paused"]
    eng.set_market_context("NVDAon", load_json("rwa_dynamic_NVDAon.json")["data"]["statusInfo"])
    assert eng.evaluate_manual(s, cs, pf()).conditions_failed == ["live_token_paused"]
    # bStock statusInfo: marketStatus null, reasonCode TRADING -> allowed
    eng.set_market_context("NVDAB", load_json("rwa_dynamic_NVDAB.json")["data"]["statusInfo"])
    assert eng.evaluate_manual(always_in("NVDAB", volume=False), extended("NVDAB"), pf()).action == Action.BUY
    paused = {**load_json("status_NVDAon.json")["data"], "marketStatus": "regular", "reasonCode": "ASSET_PAUSED"}
    eng.set_market_context("NVDAon", paused)
    assert eng.evaluate_manual(s, cs, pf()).conditions_failed == ["live_token_paused"]
    eng.set_market_context("NVDAon", {"status": "trading"})
    assert eng.evaluate_manual(s, cs, pf()).action == Action.BUY
    eng.set_market_context("NVDAon", {"status": "trading", "reference_price_only": True})
    assert eng.evaluate_manual(s, cs, pf()).conditions_failed == ["live_token_paused"]
    eng.set_market_context("NVDAon", None)
    assert eng.market_context("NVDAon") is None and eng.evaluate_manual(s, cs, pf()).action == Action.BUY


@pytest.mark.parametrize(
    "status", ["paused", "closed", "PAUSED", "off-hours", "offhours", "no liquidity", "no_liquidity", "Off Hours"]
)
def test_market_context_paused_blocks_buys_not_sells(status):
    eng = live(market_context={"NVDAB": {"status": status}})
    sig = eng.evaluate_manual(always_in("NVDAB", volume=False), extended("NVDAB"), pf())
    assert sig.action == Action.STAY_OUT and sig.conditions_failed == ["live_token_paused"]
    auto = eng.evaluate_auto(
        AutoConfig(symbol="NVDAB", capital=1000, style=TraderStyle.BALANCED), extended("NVDAB"), pf()
    )
    assert auto.action == Action.STAY_OUT  # grid would BUY here (see test_auto_on_real_samples)
    assert eng.evaluate_manual(always_in("NVDAB"), extended("NVDAB"), pf()).action == Action.STAY_OUT
    assert (
        live(market_context={"NVDAB": {"status": "open"}})
        .evaluate_manual(always_in("NVDAB", volume=False), extended("NVDAB"), pf())
        .action
        == Action.BUY
    )


def test_price_impact_gate_matches_risk_1pct():
    s = always_in("NVDAB", volume=False)
    cs = extended("NVDAB")

    def run(ctx, **cfg):
        return live(market_context={"NVDAB": ctx}, **cfg).evaluate_manual(s, cs, pf())

    # > 1% -> STAY_OUT, no size-halving band any more
    for ctx in ({"price_impact_pct": 1.5}, {"priceImpactPercent": "1.01"}, {"priceImpactPercent": 2.5}):
        sig = run({"status": "trading", **ctx})
        assert sig.action == Action.STAY_OUT and sig.conditions_failed == ["live_price_impact"]
    # <= 1% -> full live-safe size (20 USDT on 1000), strings parsed with float()
    for imp in ("0.35", 0.35, "1", 1.0, "0.0001", 0, "0", -0.2):
        sig = run({"status": "trading", "priceImpactPercent": imp})
        assert sig.action == Action.BUY and sig.proposed_notional == pytest.approx(20.0), imp
        assert "impact" not in sig.reason
    # null / unparseable = unknown: no block, no halving
    for ctx in ({"priceImpactPercent": None}, {"priceImpactPercent": "n/a"}, {"price_impact_pct": "nan"}, {}):
        sig = run(ctx)
        assert sig.action == Action.BUY and sig.proposed_notional == pytest.approx(20.0)
    # either key is accepted; the first parseable one wins
    assert run({"price_impact_pct": None, "priceImpactPercent": "1.2"}).conditions_failed == ["live_price_impact"]
    assert run({"price_impact_pct": "0.2", "priceImpactPercent": "5"}).action == Action.BUY
    # configurable threshold
    assert run({"priceImpactPercent": "1.5"}, max_price_impact_pct=2.0).action == Action.BUY
    # Ondo at 1.5%: blocked by the impact gate (no halving, so no min-order interaction)
    o = live(market_context={"NVDAon": {"priceImpactPercent": "1.5"}})
    sig = o.evaluate_manual(always_in("NVDAon", volume=False), extended("NVDAon"), pf())
    assert sig.conditions_failed == ["live_price_impact"]
    o = live(market_context={"NVDAon": {"priceImpactPercent": "0.35"}})
    assert o.evaluate_manual(always_in("NVDAon", volume=False), extended("NVDAon"), pf()).proposed_notional == 20.0
    # Without live_safe the impact gate is off (sizes unchanged); token status still gates
    plain = DefaultStrategyEngine(market_context={"NVDAB": {"price_impact_pct": 1.5}})
    p = plain.evaluate_manual(s, cs, pf())
    assert p.action == Action.BUY and p.proposed_size_pct == pytest.approx(10.0)
    assert normalize_market_context({"priceImpactPercent": "nan", "status": None}) == {
        "status": None,
        "reference_price_only": False,
        "open_state": None,
        "reason_code": None,
        "price_impact_pct": None,
        "reference_price": None,
        "equity": None,
    }


def test_config_validation_new_fields():
    for bad in (
        {"max_order_usdt": 0},
        {"min_order_usdt": {"ondo": -1}},
        {"equity_hint_usdt": 0},
        {"max_price_impact_pct": 0},
    ):
        with pytest.raises(ValueError):
            LiveSafetyConfig(**bad)
