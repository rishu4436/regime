"""Narrative tags for Auto-pick, the optional Grok fallback, pick wording and cap wording. Offline only."""
import importlib.util
import re
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from backend.contracts import (
    Action, AgentMode, Candle, Decision, Regime, RiskCheck, RiskVerdict, Signal, TradingMode,
)
from backend.llm import DefaultExplainer, live, templates
from backend.llm import narratives as nv
from backend.llm.narratives import (
    NARRATIVE_LABELS, explain_picks, make_narrative_of, narrative_of, parse_narrative_code, underlying_ticker,
)

RAW_CODE = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")


@pytest.fixture(autouse=True)
def _reset_limits():
    templates.set_live_limits(None)
    yield
    templates.set_live_limits(None)


# ------------------------------------------------------------------ sector map

@pytest.mark.parametrize("sym,want", [
    ("NVDAB", "AI_CHIPS"), ("NVDAon", "AI_CHIPS"), ("NVDAx", "AI_CHIPS"), ("NVDA", "AI_CHIPS"), ("nvdab", "AI_CHIPS"),
    ("TSLAB", "EV_ENERGY"), ("TSLAx", "EV_ENERGY"), ("XOMon", "EV_ENERGY"),
    ("AAPLon", "BIG_TECH"), ("GOOGLx", "BIG_TECH"), ("GOOGB", "BIG_TECH"), ("METAB", "BIG_TECH"),
    ("COINB", "CRYPTO"), ("MSTRon", "CRYPTO"), ("CRCLx", "CRYPTO"),
    ("SPYB", None), ("QQQon", None), ("VOOx", None), ("IVV", None),
    ("FOOB", None), ("UNKNOWN", None), ("", None),
])
def test_symbol_variants(sym, want):
    assert narrative_of(sym) == want


def test_no_false_strip_of_real_ticker_letters():
    # Real tickers that end in B / X / ON are not cut down to something else.
    assert underlying_ticker("ARB") is None and narrative_of("ARB") is None        # not "AR"
    assert underlying_ticker("BOX") is None and narrative_of("BOX") is None        # not "BO"
    assert underlying_ticker("MON") is None and narrative_of("MON") is None        # not "M"
    assert underlying_ticker("ARM") == "ARM"                                       # full ticker wins
    assert underlying_ticker("ARMB") == "ARM" and underlying_ticker("AMDx") == "AMD"
    assert narrative_of("NIO") == "EV_ENERGY" and narrative_of("NIOB") == "EV_ENERGY"


def test_name_hint_fallback():
    assert narrative_of("ZZZB", "NVIDIA Corporation (Ondo Tokenized)") == "AI_CHIPS"
    assert narrative_of("ZZZB", "Coinbase Global") == "CRYPTO"
    assert narrative_of("ZZZB", "Intelligent Widgets Inc") is None   # no partial-word match on "intel"
    assert narrative_of("ZZZB", "SPDR S&P 500 ETF Trust") is None
    assert narrative_of("TSLAB", "NVIDIA") == "EV_ENERGY"           # the symbol wins over the name


def test_labels():
    assert NARRATIVE_LABELS == {"AI_CHIPS": "AI & chips", "EV_ENERGY": "EV & energy", "BIG_TECH": "Big Tech",
                                "CRYPTO": "Crypto-linked", "ANY": "Any"}


# ------------------------------------------------------------------ Grok fallback

class FakeClient:
    def __init__(self, answer="AI_CHIPS", enabled=True, exc=None):
        self.answer, self.enabled, self.exc, self.calls = answer, enabled, exc, []

    def chat(self, messages, **kw):
        self.calls.append((messages, kw))
        if self.exc:
            raise self.exc
        return self.answer


def test_grok_fallback_valid_code_and_cached():
    c = FakeClient("ai_chips.")
    f = make_narrative_of(c)
    assert f("ZZZB") == "AI_CHIPS" and f("zzzb") == "AI_CHIPS" and f("ZZZB", "whatever") == "AI_CHIPS"
    assert len(c.calls) == 1
    assert c.calls[0][1]["temperature"] == 0.0


@pytest.mark.parametrize("junk", ["AI chips", "AI_CHIPS, maybe EV_ENERGY", "I think CRYPTO", "NONE", "SPACE", ""])
def test_grok_junk_is_none_and_cached(junk):
    c = FakeClient(junk)
    f = make_narrative_of(c)
    assert f("QWERB") is None and f("QWERB") is None
    assert len(c.calls) == 1


def test_grok_error_is_none():
    c = FakeClient(exc=RuntimeError("timeout"))
    assert make_narrative_of(c)("QWERB") is None


def test_grok_never_called_for_mapped_or_etf_or_name_hint():
    c = FakeClient("CRYPTO")
    f = make_narrative_of(c)
    assert f("NVDAB") == "AI_CHIPS" and f("SPYB") is None and f("ZZZB", "Tesla Inc") == "EV_ENERGY"
    assert c.calls == []


def test_grok_never_called_without_key(monkeypatch):
    c = FakeClient("CRYPTO", enabled=False)
    assert make_narrative_of(c)("QWERB") is None and c.calls == []
    # default: reads the env; no XAI_API_KEY (conftest clears it) -> map only, no network at all
    def boom(*a, **k):
        raise AssertionError("network used")
    monkeypatch.setattr(httpx.Client, "post", boom)
    assert make_narrative_of()("QWERB") is None
    assert DefaultExplainer().narrative_of()("QWERB") is None


def test_grok_via_real_client_uses_short_timeout(make_client, reply):
    client, rec = make_client(reply("BIG_TECH"), timeout_s=20.0)
    f = make_narrative_of(DefaultExplainer(client=client))
    assert f("QWERB") == "BIG_TECH" and f("QWERB") == "BIG_TECH"
    assert len(rec.requests) == 1
    assert rec.payloads[0]["max_tokens"] == 10
    assert rec.requests[0].extensions["timeout"]["read"] <= nv.GROK_TIMEOUT_S


def test_parse_narrative_code():
    assert parse_narrative_code('"EV_ENERGY"') == "EV_ENERGY"
    assert parse_narrative_code("ev_energy\n") == "EV_ENERGY"
    assert parse_narrative_code("ANY") is None and parse_narrative_code(None) is None


# ------------------------------------------------------------------ pick wording

BOT2_PICKS = [
    {"symbol": "NVDAB", "narrative": "AI_CHIPS", "score": 81.2,
     "reason": "AI & chips; up 6.2% over 20 bars; above a rising 50-bar average; 24h volume 1.2M USDT; market strong uptrend"},
    {"symbol": "AMDB", "narrative": "AI_CHIPS", "score": 20.1,
     "reason": "AI & chips; down 3.0% over 20 bars; below a falling 50-bar average; market downtrend, ranked last"},
    {"symbol": "SPYB", "narrative": "ANY", "score": 50.0,
     "reason": "up 1.0% over 20 bars; above a flat 50-bar average; market SIDEWAYS"},
]
BOT2_SKIPPED = [
    {"symbol": "TSLAx", "reason": "xStock tokens have almost no liquidity on BNB Chain"},
    {"symbol": "COINon", "reason": "minimum order 50 USDT is above the 200 USDT per-order cap"},
    {"symbol": "MSFTB", "reason": "not enough price history (12 bars, 61 needed) or market closed"},
    {"symbol": "AAPLB", "reason": "live_token_paused"},
]


def test_explain_picks_wording():
    out = explain_picks(BOT2_PICKS, BOT2_SKIPPED)
    assert out[0] == ("Picked NVDAB (AI & chips): up 6.2% over 20 bars, above a rising 50-bar average, "
                      "24h volume 1.2M USDT, market strong uptrend.")
    assert out[1] == ("Picked AMDB (AI & chips): down 3.0% over 20 bars, below a falling 50-bar average, "
                      "market downtrend, ranked last.")
    assert out[2] == "Picked SPYB: up 1.0% over 20 bars, above a flat 50-bar average, market sideways."
    assert out[3] == "Skipped TSLAx: xStock tokens have almost no liquidity on BNB Chain."
    assert out[4] == "Skipped COINon: minimum order 50 USDT is above the 200 USDT per-order cap."
    assert out[5] == "Skipped MSFTB: not enough price history (12 bars, 61 needed) or market closed."
    assert out[6].startswith("Skipped AAPLB: the token is paused")
    for line in out:
        assert not RAW_CODE.search(line), line
        assert line.count("AI & chips") <= 1


def test_explain_picks_accepts_objects_and_rank_result_shape():
    class P:
        def __init__(self, d):
            self.__dict__.update(d)

    class R:
        picks = [P(BOT2_PICKS[0])]
        skipped = BOT2_SKIPPED[:1]

    assert explain_picks(R()) == explain_picks(BOT2_PICKS[:1], BOT2_SKIPPED[:1])
    assert explain_picks([]) == [] and explain_picks([], None) == []
    assert DefaultExplainer().explain_picks(BOT2_PICKS[:1]) == explain_picks(BOT2_PICKS[:1])


# ------------------------------------------------------------------ per-order cap wording follows live limits

def _blocked_live(reasons, checks=()):
    sig = Signal(symbol="NVDAB", action=Action.BUY, source=AgentMode.AUTO, strategy="TREND_FOLLOWING",
                 regime=Regime.STRONG_UPTREND, reason="trend entry", price=200.0, proposed_notional=300.0)
    v = RiskVerdict(signal_id=sig.id, approved=False, reasons=list(reasons), checks=list(checks))
    return Decision(mode=TradingMode.LIVE, agent_mode=AgentMode.AUTO, symbol="NVDAB", regime=Regime.STRONG_UPTREND,
                    strategy="TREND_FOLLOWING", signal=sig, action=Action.STAY_OUT, risk_verdict=v, reason="blocked")


def test_cap_wording_uses_raised_cap_from_live_limits():
    DefaultExplainer().set_live_limits({"per_order_cap_usdt": 200.0})
    assert live.plain_reason("live_order_cap") == "the per-order cap of 200 USDT (set by Regime's risk limits)"
    assert "per-order cap of 200 USDT" in live.plain_reason("live_below_min_order")
    txt = templates.explain_decision(_blocked_live(["live_order_cap"]))
    assert "200 USDT" in txt and "25" not in txt
    # a detail that carries its own number is shown as-is (no second number added)
    d = _blocked_live([], [RiskCheck(name="live_order_cap", passed=False, detail="LIVE per-order cap 200 USDT")])
    txt = templates.explain_decision(d)
    assert "per-order cap" in txt and "200 USDT" in txt and "of 200 USDT" not in txt and "25" not in txt


def test_cap_wording_without_limits_has_no_number():
    assert live.plain_reason("live_order_cap") == "the per-order cap (set by Regime's risk limits)"
    templates.set_live_limits({"per_order_cap_usdt": None})
    assert "USDT" not in live.plain_reason("live_order_cap")
    templates.set_live_limits({"per_order_cap_usdt": 25})
    assert "of 25 USDT" in live.plain_reason("live_order_cap")
    templates.set_live_limits({"per_order_cap_usdt": 200})
    assert "of 200 USDT" in live.plain_reason("live_order_cap")


def test_llm_package_has_no_hardcoded_order_cap():
    import pathlib

    src = pathlib.Path(live.__file__).parent
    for p in src.glob("*.py"):
        text = p.read_text()
        assert not re.search(r"(?:cap|max_order\w*|per_order\w*)\s*=\s*25\b", text), p.name


# ------------------------------------------------------------------ with the engine's rank_tokens (if present)

def _candles(n=120, start=100.0, step=0.4, vol=5000.0):
    t0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
    out, p = [], start
    for i in range(n):
        o, c = p, p + step + (0.3 if i % 2 else -0.2)
        out.append(Candle(timestamp=t0 + timedelta(hours=i), open=o, high=max(o, c) * 1.002, low=min(o, c) * 0.998,
                          close=c, volume=vol))
        p = c
    return out


@pytest.mark.skipif(importlib.util.find_spec("backend.engine.autopick") is None,
                    reason="backend.engine.autopick not on this branch yet")
def test_rank_tokens_with_narrative_of_and_wording():
    from backend.engine.autopick import rank_tokens

    tokens = [{"symbol": s} for s in ("NVDAB", "AMDon", "TSLAB", "AAPLon", "SPYB", "NVDAx")]
    candles = {s: _candles() for s in ("NVDAB", "AMDon", "TSLAB", "AAPLon", "SPYB")}
    res = rank_tokens(tokens, candles, "AI & chips", 3, narrative_of=narrative_of)
    syms = {p.symbol for p in res.picks}
    assert syms <= {"NVDAB", "AMDon"} and syms
    lines = explain_picks(res.picks, res.skipped)
    assert all(line.startswith(("Picked ", "Skipped ")) for line in lines)
    assert any("NVDAx" in line for line in lines if line.startswith("Skipped"))
    for line in lines:
        assert not RAW_CODE.search(line), line
    assert all(line.count("AI & chips") == 1 for line in lines if line.startswith("Picked"))
    # the Grok-backed callable is a drop-in too
    res2 = rank_tokens(tokens, candles, "ANY", 5, narrative_of=make_narrative_of(FakeClient(enabled=False)))
    assert len(res2.picks) >= 4


def test_watchlist_risk_codes_are_plain():
    from backend.llm import live
    for code in ("live_token_exposure", "reserved_orders"):
        txt = live.REASON_TEXT[code]
        assert "_" not in txt and len(txt) > 20
