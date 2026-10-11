"""Auto-pick ranking and watchlist evaluation."""

from __future__ import annotations

import pytest

from backend.contracts import Action, AgentMode, AutoConfig, Portfolio, Regime, Signal
from backend.engine import DefaultStrategyEngine
from backend.engine.autopick import (
    MAX_WATCHLIST,
    evaluate_watchlist,
    normalize_narrative,
    rank_tokens,
    score_token,
)

from .helpers import bar, sma_entry_strategy


def trend(n: int = 120, pct: float = 0.3, start: float = 100.0, v: float = 1000.0) -> list:
    """Bars that close pct% higher (or lower) each bar, with a small range."""
    cs, p = [], start
    for i in range(n):
        c = p * (1 + pct / 100)
        cs.append(bar(i, p, max(p, c) * 1.001, min(p, c) * 0.999, c, v))
        p = c
    return cs


def wiggle(n: int = 120, price: float = 100.0, amp: float = 0.2, v: float = 1000.0) -> list:
    cs = []
    for i in range(n):
        c = price * (1 + (amp / 100) * (1 if i % 2 else -1))
        cs.append(bar(i, price, max(price, c) * 1.001, min(price, c) * 0.999, c, v))
    return cs


def pf() -> Portfolio:
    return Portfolio(starting_capital=100, cash=100, equity=100)


SECTORS = {"NVDAB": "AI & chips", "AMDB": "AI_CHIPS", "TSLAB": "EV & energy", "AAPLB": "Big Tech", "COINB": "Crypto-linked"}


def test_normalize_narrative():
    assert normalize_narrative("AI & chips") == "AI_CHIPS"
    assert normalize_narrative("ai_chips") == "AI_CHIPS"
    assert normalize_narrative(None) == normalize_narrative("") == normalize_narrative("any") == "ANY"
    assert normalize_narrative("Crypto-linked") == "CRYPTO"
    assert normalize_narrative("Space stocks") == "SPACE_STOCKS"


def test_uptrend_beats_sideways_beats_downtrend():
    data = {"NVDAB": trend(pct=0.3), "AAPLB": wiggle(), "TSLAB": trend(pct=-0.4)}
    res = rank_tokens(["TSLAB", "AAPLB", "NVDAB"], data, "ANY", 3)
    assert [p.symbol for p in res.picks] == ["NVDAB", "AAPLB", "TSLAB"]
    assert res.picks[-1].regime == Regime.DOWNTREND and "ranked last" in res.picks[-1].reason
    assert "up " in res.picks[0].reason and "above a rising 50-bar average" in res.picks[0].reason
    assert all(0 <= p.score <= 100 for p in res.picks)


def test_downtrend_ranks_last_even_with_more_liquidity():
    data = {"NVDAB": wiggle(v=10), "TSLAB": trend(pct=-0.4, v=10_000_000)}
    res = rank_tokens(["TSLAB", "NVDAB"], data, "ANY", 2)
    assert [p.symbol for p in res.picks] == ["NVDAB", "TSLAB"]


def test_narrative_filter_uses_sector_map():
    data = {s: trend() for s in SECTORS}
    res = rank_tokens(list(SECTORS), data, "AI & chips", 5, narrative_of=SECTORS.get)
    assert sorted(p.symbol for p in res.picks) == ["AMDB", "NVDAB"]
    assert all(p.narrative == "AI_CHIPS" and p.reason.startswith("AI & chips") for p in res.picks)
    assert res.skipped == []  # other narratives are not candidates, not "skipped"


def test_narrative_falls_back_to_token_field_and_survives_lookup_errors():
    def boom(_s):
        raise RuntimeError("sector map down")

    toks = [{"symbol": "NVDAB", "narrative": "AI & chips"}, {"symbol": "AAPLB", "narrative": "Big Tech"}]
    res = rank_tokens(toks, {"NVDAB": trend(), "AAPLB": trend()}, "BIG_TECH", 3, narrative_of=boom)
    assert [p.symbol for p in res.picks] == ["AAPLB"]


def test_max_tokens_limits_picks_and_is_validated():
    data = {f"T{i}B": trend(pct=0.1 * (i + 1)) for i in range(7)}
    res = rank_tokens(list(data), data, "ANY", 2)
    assert len(res.picks) == 2 and len(res.ranked) == 7
    assert res.picks[0].symbol == "T6B"  # strongest momentum first
    for bad in (0, MAX_WATCHLIST + 1):
        with pytest.raises(ValueError):
            rank_tokens(list(data), data, "ANY", bad)


@pytest.mark.parametrize(
    "meta,why",
    [
        ({"symbol": "NVDAB", "status": "paused"}, "not trading right now"),
        ({"symbol": "NVDAon", "status": "OFFHOURS"}, "not trading right now"),
        ({"symbol": "NVDAB", "tradable": False}, "not tradable"),
        ({"symbol": "NVDAB", "min_order_usdt": 50}, "above the 25 USDT per-order cap"),
        ({"symbol": "TSLAx"}, "almost no liquidity"),
    ],
)
def test_untradable_tokens_are_skipped_with_a_reason(meta, why):
    res = rank_tokens([meta], {meta["symbol"]: trend()}, "ANY", 1)
    assert res.picks == [] and len(res.skipped) == 1 and why in res.skipped[0]["reason"]


def test_short_history_is_skipped():
    res = rank_tokens(["NVDAB", "AAPLB"], {"NVDAB": trend(n=30), "AAPLB": trend()}, "ANY", 2)
    assert [p.symbol for p in res.picks] == ["AAPLB"]
    assert res.skipped[0]["symbol"] == "NVDAB" and "not enough price history" in res.skipped[0]["reason"]


def test_missing_candles_and_duplicates():
    res = rank_tokens(["NVDAB", "NVDAB", {"symbol": ""}], {}, "ANY", 1)
    assert res.picks == [] and len(res.skipped) == 1


def test_liquidity_from_metadata_raises_score():
    lo, _ = score_token("NVDAB", trend(), {"volume_24h": 2_000})
    hi, _ = score_token("NVDAB", trend(), {"volume_24h": 5_000_000})
    assert hi.score > lo.score and "24h volume 5.0M USDT" in hi.reason


def test_to_dict_shape_matches_ui_contract():
    res = rank_tokens(["NVDAB"], {"NVDAB": trend()}, "ANY", 1)
    assert set(res.picks[0].to_dict()) == {"symbol", "narrative", "score", "reason"}
    assert set(res.to_dict()) == {"picks", "ranked", "skipped"}


def test_ranking_is_deterministic():
    data = {s: trend() for s in ("BBB", "AAA", "CCC")}
    a = rank_tokens(list(data), data, "ANY", 3)
    b = rank_tokens(list(reversed(list(data))), data, "ANY", 3)
    assert [p.symbol for p in a.picks] == [p.symbol for p in b.picks] == ["AAA", "BBB", "CCC"]


# ------------------------------------------------------------------ evaluate_watchlist


class FakeEngine:
    def __init__(self, actions):
        self.actions, self.ctx = actions, {}

    def set_market_context(self, sym, ctx):
        self.ctx[sym] = ctx

    def evaluate_auto(self, cfg, candles, portfolio):
        a = self.actions[cfg.symbol]
        if a == "boom":
            raise RuntimeError("bad data")
        return Signal(symbol=cfg.symbol, action=a, source=AgentMode.AUTO, strategy="X", reason="r")

    evaluate_manual = evaluate_auto


def test_watchlist_orders_sells_then_best_buys_then_stay_outs():
    eng = FakeEngine({"A": Action.STAY_OUT, "B": Action.BUY, "C": Action.SELL, "D": Action.BUY, "E": "boom"})
    cfg = AutoConfig(symbol="A", capital=100)
    sigs = evaluate_watchlist(
        eng, ["A", "B", "C", "D", "E"], {}, pf(), auto_config=cfg, scores={"B": 40, "D": 70},
        market_contexts={"B": {"status": "trading"}},
    )
    assert [(s.symbol, s.action) for s in sigs] == [
        ("C", Action.SELL), ("D", Action.BUY), ("B", Action.BUY), ("A", Action.STAY_OUT), ("E", Action.STAY_OUT)
    ]
    assert sigs[-1].conditions_failed == ["evaluation_error"] and "bad data" in sigs[-1].reason
    assert eng.ctx["B"] == {"status": "trading"} and eng.ctx["A"] is None


def test_watchlist_caps_at_five_and_dedupes():
    syms = ["A", "A", "B", "C", "D", "E", "F"]
    eng = FakeEngine({s: Action.STAY_OUT for s in syms})
    sigs = evaluate_watchlist(eng, syms, {}, pf(), auto_config=AutoConfig(symbol="A", capital=100))
    assert [s.symbol for s in sigs] == ["A", "B", "C", "D", "E"]


def test_watchlist_needs_exactly_one_template():
    with pytest.raises(ValueError):
        evaluate_watchlist(FakeEngine({}), ["A"], {}, pf())
    with pytest.raises(ValueError):
        evaluate_watchlist(
            FakeEngine({}), ["A"], {}, pf(), auto_config=AutoConfig(symbol="A", capital=1),
            manual_strategy=sma_entry_strategy(),
        )


def test_watchlist_with_real_engine_manual():
    eng = DefaultStrategyEngine()
    data = {"NVDAB": trend(), "AAPLB": trend(pct=-0.4)}
    sigs = evaluate_watchlist(eng, ["AAPLB", "NVDAB", "TSLAB"], data, pf(), manual_strategy=sma_entry_strategy())
    by = {s.symbol: s for s in sigs}
    assert set(by) == {"AAPLB", "NVDAB", "TSLAB"}
    assert by["NVDAB"].action == Action.BUY and by["NVDAB"].source == AgentMode.MANUAL
    assert by["TSLAB"].action == Action.STAY_OUT  # no candles
    assert sigs[0].symbol == "NVDAB"


def test_watchlist_with_real_engine_auto():
    eng = DefaultStrategyEngine()
    data = {"NVDAB": trend(), "AAPLB": wiggle()}
    sigs = evaluate_watchlist(eng, ["AAPLB", "NVDAB"], data, pf(), auto_config=AutoConfig(symbol="X", capital=100))
    assert {s.symbol for s in sigs} == {"AAPLB", "NVDAB"} and all(s.source == AgentMode.AUTO for s in sigs)


# ------------------------------------------------------------------ user-set live limits (big wallets)


def test_set_live_limits_lets_big_wallets_size_up():
    from backend.engine.live import live_size

    eng = DefaultStrategyEngine(live_safe=True)
    data = trend()
    before = live_size("NVDAB", 10, 10_000, data, eng.live_config, user_sized=True)
    assert before.notional == pytest.approx(25.0)  # default 25 USDT per-order cap
    eng.set_live_limits(max_order_usdt=200)
    after = live_size("NVDAB", 10, 10_000, data, eng.live_config, user_sized=True)
    assert after.notional == pytest.approx(200.0)
    eng.set_live_limits(max_size_pct=5)
    assert eng.live_config.max_size_pct == 5 and eng.live_config.max_order_usdt == 200
    for bad in ({"max_order_usdt": 0}, {"max_size_pct": 0}, {"max_size_pct": 101}):
        with pytest.raises(ValueError):
            eng.set_live_limits(**bad)


def test_auto_live_size_uses_real_equity_not_allocated_capital():
    """Rishu's PC: AUTO/GRID with a large AutoConfig.capital on a ~110 USDT wallet proposed 25 USDT (2% of the
    allocation, hitting the 25 cap). The basis must be min(capital, equity): 2% of 109.87 -> raised to the 10 USDT
    NVDAB minimum."""
    from datetime import timedelta

    cs = wiggle(n=120)
    cs.append(bar(120, 99.8, 99.8, 99.74, 99.75, 1000.0))  # bottom grid cell, still inside the range
    now = cs[-1].timestamp + timedelta(minutes=30)
    port = Portfolio(starting_capital=109.87, cash=99.87, equity=109.87)
    eng = DefaultStrategyEngine(live_safe=True, clock=lambda: now)
    eng.set_market_context("NVDAB", {"status": "trading", "min_order_usdt": 10})
    sig = eng.evaluate_auto(AutoConfig(symbol="NVDAB", capital=10_000), cs, port)
    assert sig.action == Action.BUY and "bottom grid" in sig.reason, sig.reason
    assert sig.proposed_notional == pytest.approx(10.0), sig.reason
    big = Portfolio(starting_capital=10_000, cash=10_000, equity=10_000)
    assert eng.evaluate_auto(AutoConfig(symbol="NVDAB", capital=10_000), cs, big).proposed_notional == pytest.approx(25.0)
    assert DefaultStrategyEngine._auto_basis(AutoConfig(symbol="X", capital=50), port) == 50


def test_candidate_tokens_prefilters_the_full_list():
    from backend.engine.autopick import candidate_tokens

    rows = [{"symbol": f"T{i}B", "volume_24h": i * 1000, "status": "trading", "narrative": "AI & chips"} for i in range(30)]
    rows += [
        {"symbol": "CLOSEDon", "status": "closed", "volume_24h": 10**9, "narrative": "AI & chips"},
        {"symbol": "NVDAx", "tradable": False, "volume_24h": 10**9},
        {"symbol": "BIGB", "min_order_usdt": 100, "volume_24h": 10**9, "narrative": "AI & chips"},
        {"symbol": "AAPLB", "volume_24h": 10**8, "narrative": "Big Tech"},
    ]
    top = candidate_tokens(rows, "AI_CHIPS", limit=5)
    assert [r["symbol"] for r in top] == ["T29B", "T28B", "T27B", "T26B", "T25B"]
    assert [r["symbol"] for r in candidate_tokens(rows, "ANY", limit=1)] == ["AAPLB"]
    assert [r["symbol"] for r in candidate_tokens(rows, "BIG_TECH", narrative_of=lambda s: None)] == ["AAPLB"]
    raised = candidate_tokens(rows, "ANY", limit=1, cfg=__import__("backend.engine.autopick", fromlist=["x"]).AutoPickConfig(max_order_usdt=200))
    assert raised[0]["symbol"] == "BIGB"  # a raised per-order cap makes the 100 USDT minimum buyable


def test_zero_volume_token_is_not_picked():
    """Live preview (Bot 4, 11 Oct): PLTRB ranked first with '24h volume 0 USDT'. A known 0 volume is ineligible;
    an unknown (None) volume still is not."""
    from backend.engine.autopick import AutoPickConfig, candidate_tokens, rank_tokens

    up = wiggle(n=120)
    toks = [
        {"symbol": "PLTRB", "status": "trading", "volume_24h": 0},
        {"symbol": "NVDAB", "status": "trading", "volume_24h": 25_000},
        {"symbol": "AMDB", "status": "trading", "volume_24h": None},
    ]
    res = rank_tokens(toks, {t["symbol"]: up for t in toks}, "ANY", 3)
    picked = [p.symbol for p in res.picks]
    assert "PLTRB" not in picked and set(picked) == {"NVDAB", "AMDB"}, picked
    skip = {s["symbol"]: s["reason"] for s in res.skipped}
    assert "no trading volume" in skip["PLTRB"], skip
    assert [t["symbol"] for t in candidate_tokens(toks)] == ["NVDAB", "AMDB"]
    strict = AutoPickConfig(min_volume_24h_usdt=50_000)
    res2 = rank_tokens(toks, {t["symbol"]: up for t in toks}, "ANY", 3, cfg=strict)
    assert [p.symbol for p in res2.picks] == ["AMDB"]
    assert "below the 50,000 USDT minimum" in {s["symbol"]: s["reason"] for s in res2.skipped}["NVDAB"]
