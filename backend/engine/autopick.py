"""Auto-pick and watchlist evaluation (multi-token trading).

``rank_tokens`` picks 1-5 tokens for the watchlist: it drops tokens that can't be traded now, keeps those matching
the chosen narrative, and ranks the rest on momentum, trend and liquidity. Tokens in a DOWNTREND or HIGH_VOLATILITY
regime are kept but rank below every other token, so Auto-pick does not fill the watchlist with falling stocks.

``evaluate_watchlist`` runs the engine once per watched token and orders the signals: SELLs first (reducing exposure
always wins), then BUYs best score first (so if risk only allows one more position it goes to the strongest token),
then STAY_OUTs. The risk layer still decides every trade.

Token metadata is a mapping per token (Bot 4's token list). Keys used, all optional except ``symbol``:
``status`` / ``tradable``, ``min_order_usdt`` (or ``minOrderUsdt``), ``volume_24h`` (or ``volume24h``),
``liquidity``, ``narrative``. Unknown values never exclude a token; they only score lower.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Sequence

from backend.contracts import Action, AgentMode, AutoConfig, Candle, ManualStrategy, Portfolio, Regime, Signal

from .data import sanitize_candles, token_family
from .live import PAUSED_STATUSES, LiveSafetyConfig
from .regime import MIN_BARS, detect_regime

ANY = "ANY"
NARRATIVES = ("AI_CHIPS", "EV_ENERGY", "BIG_TECH", "CRYPTO", ANY)
NARRATIVE_LABELS = {
    "AI_CHIPS": "AI & chips",
    "EV_ENERGY": "EV & energy",
    "BIG_TECH": "Big Tech",
    "CRYPTO": "Crypto-linked",
    ANY: "Any",
}
MAX_WATCHLIST = 5
MOMENTUM_BARS = 20
WEAK_REGIMES = frozenset({Regime.DOWNTREND, Regime.HIGH_VOLATILITY})
REGIME_WORDS = {
    Regime.STRONG_UPTREND: "strong uptrend",
    Regime.MILD_UPTREND: "mild uptrend",
    Regime.SIDEWAYS: "sideways",
    Regime.OVERSOLD_PULLBACK: "oversold pullback",
    Regime.HIGH_VOLATILITY: "high volatility",
    Regime.DOWNTREND: "downtrend",
}


@dataclass(frozen=True)
class AutoPickConfig:
    max_order_usdt: float = 25.0  # a token whose minimum order is above this can never be bought (risk's order cap)
    blocked_families: frozenset = LiveSafetyConfig().blocked_families  # xStock: ~no liquidity on BSC
    momentum_cap_pct: float = 10.0  # +/-10% over MOMENTUM_BARS maps to the full momentum score
    liquidity_floor_usdt: float = 1_000.0  # log scale: 1k USDT 24h volume -> 0, 10M -> full liquidity score
    liquidity_full_usdt: float = 10_000_000.0
    weight_momentum: float = 40.0
    weight_trend: float = 35.0
    weight_liquidity: float = 25.0
    weak_regime_factor: float = 0.3  # downtrend / high volatility scores are multiplied by this


@dataclass
class TokenPick:
    symbol: str
    narrative: str
    score: float
    reason: str
    regime: Optional[Regime] = None
    features: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"symbol": self.symbol, "narrative": self.narrative, "score": self.score, "reason": self.reason}


@dataclass
class RankResult:
    picks: list[TokenPick]  # the chosen tokens, best first (at most max_tokens)
    ranked: list[TokenPick]  # every eligible token, best first
    skipped: list[dict]  # [{symbol, reason}] for tokens that could not be picked

    def to_dict(self) -> dict:
        return {
            "picks": [p.to_dict() for p in self.picks],
            "ranked": [p.to_dict() for p in self.ranked],
            "skipped": list(self.skipped),
        }


def _num(v: Any) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _first(meta: Mapping[str, Any], *keys: str) -> Any:
    for k in keys:
        if meta.get(k) is not None:
            return meta[k]
    return None


def normalize_narrative(n: Optional[str]) -> str:
    """'AI & chips', 'ai_chips', 'AI chips' -> 'AI_CHIPS'; None / '' / 'any' -> 'ANY'. Unknown names are kept
    (upper-cased) so a new narrative from the sector map still matches itself."""
    if not n:
        return ANY
    s = str(n).strip()
    for key, label in NARRATIVE_LABELS.items():
        if s.lower() in (key.lower(), label.lower()):
            return key
    return "_".join(s.replace("&", " ").replace("-", " ").upper().split()) or ANY


def _blocker(symbol: str, meta: Mapping[str, Any], cfg: AutoPickConfig) -> Optional[str]:
    status = _first(meta, "status")
    if status is not None and str(status).lower() in PAUSED_STATUSES:
        return f"not trading right now (status {str(status).lower()})"
    if meta.get("tradable") is False:
        return "not tradable right now"
    fam = token_family(symbol)
    if fam in cfg.blocked_families:
        return f"{fam} tokens have almost no liquidity on BNB Chain"
    mo = _num(_first(meta, "min_order_usdt", "minOrderUsdt"))
    if mo is not None and mo > cfg.max_order_usdt:
        return f"minimum order {mo:g} USDT is above the {cfg.max_order_usdt:g} USDT per-order cap"
    return None


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def score_token(
    symbol: str,
    candles: Optional[Sequence[Candle]],
    meta: Optional[Mapping[str, Any]] = None,
    narrative: str = ANY,
    cfg: Optional[AutoPickConfig] = None,
) -> tuple[Optional[TokenPick], Optional[str]]:
    """Score one token (0-100). Returns (pick, None), or (None, why it can't be picked)."""
    cfg = cfg or AutoPickConfig()
    meta = meta or {}
    why = _blocker(symbol, meta, cfg)
    if why:
        return None, why
    clean, _ = sanitize_candles(candles or [])
    ra = detect_regime(clean)
    if ra.features.get("insufficient_data"):
        return None, f"not enough price history ({len(clean)} bars, {MIN_BARS} needed) or market closed"
    f = ra.features
    closes = [c.close for c in clean]
    n = min(MOMENTUM_BARS, len(closes) - 1)
    mom_pct = (closes[-1] / closes[-1 - n] - 1) * 100
    momentum = _clamp01(0.5 + mom_pct / (2 * cfg.momentum_cap_pct))
    slope, dist = f.get("sma_slope_pct", 0.0), f.get("dist_from_sma_pct", 0.0)
    trend = _clamp01(0.5 + slope / 4) * 0.6 + _clamp01(0.5 + dist / 10) * 0.4

    vol_usdt = _num(_first(meta, "volume_24h", "volume24h"))
    liq_usdt = _num(meta.get("liquidity"))
    if vol_usdt is None:  # fall back to the candles' own quote volume (last 24 bars, close x volume)
        recent = clean[-24:]
        vol_usdt = sum(c.close * c.volume for c in recent) or None
    depth = max([v for v in (vol_usdt, liq_usdt) if v is not None], default=None)
    if depth is None or depth <= 0:
        liquidity = 0.0
    else:
        lo, hi = math.log10(cfg.liquidity_floor_usdt), math.log10(cfg.liquidity_full_usdt)
        liquidity = _clamp01((math.log10(depth) - lo) / (hi - lo))

    score = cfg.weight_momentum * momentum + cfg.weight_trend * trend + cfg.weight_liquidity * liquidity
    weak = ra.regime in WEAK_REGIMES
    if weak:
        score *= cfg.weak_regime_factor
    score = round(score, 2)

    label = NARRATIVE_LABELS.get(narrative, narrative.replace("_", " ").title()) if narrative != ANY else None
    parts = [label] if label else []
    parts.append(f"{'up' if mom_pct >= 0 else 'down'} {abs(mom_pct):.1f}% over {n} bars")
    rising = "rising" if slope > 0 else "falling" if slope < 0 else "flat"
    parts.append(f"{'above' if dist >= 0 else 'below'} a {rising} 50-bar average")
    if vol_usdt is not None:
        parts.append(f"24h volume {_money(vol_usdt)} USDT")
    parts.append(f"market {REGIME_WORDS[ra.regime]}" + (", ranked last" if weak else ""))
    feats = {"momentum_pct": round(mom_pct, 4), "sma_slope_pct": slope, "dist_from_sma_pct": dist}
    if vol_usdt is not None:
        feats["volume_24h_usdt"] = vol_usdt
    return TokenPick(symbol, narrative, score, "; ".join(parts), ra.regime, feats), None


def _money(x: float) -> str:
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(x) >= div:
            return f"{x / div:.1f}{suf}"
    return f"{x:.0f}"


def rank_tokens(
    tokens: Sequence[Any],
    candles_by_symbol: Mapping[str, Sequence[Candle]],
    narrative: Optional[str] = ANY,
    max_tokens: int = 3,
    narrative_of: Optional[Callable[[str], Optional[str]]] = None,
    cfg: Optional[AutoPickConfig] = None,
) -> RankResult:
    """Pick up to ``max_tokens`` (1-5) tokens matching ``narrative`` ('ANY' = all).

    ``tokens``: token metadata mappings (or plain symbols). ``narrative_of(symbol)``: the token's narrative (Bot 5's
    sector map); falls back to the token's own ``narrative`` field. Ties break on symbol, so results are stable.
    """
    if not 1 <= int(max_tokens) <= MAX_WATCHLIST:
        raise ValueError(f"max_tokens must be between 1 and {MAX_WATCHLIST}")
    want = normalize_narrative(narrative)
    ranked: list[TokenPick] = []
    skipped: list[dict] = []
    seen: set[str] = set()
    for t in tokens:
        meta = {"symbol": t} if isinstance(t, str) else dict(t)
        sym = str(meta.get("symbol") or "").strip()
        if not sym or sym in seen:
            continue
        seen.add(sym)
        tag = None
        if narrative_of is not None:
            try:
                tag = narrative_of(sym)
            except Exception:  # a failing sector lookup must not break the ranking
                tag = None
        tag = normalize_narrative(tag or meta.get("narrative"))
        if want != ANY and tag != want:
            continue  # other narratives are simply not candidates; not reported as skipped
        pick, why = score_token(sym, candles_by_symbol.get(sym), meta, tag if tag != ANY else want, cfg)
        if pick is None:
            skipped.append({"symbol": sym, "reason": why})
        else:
            ranked.append(pick)
    ranked.sort(key=lambda p: (p.regime in WEAK_REGIMES, -p.score, p.symbol))
    return RankResult(ranked[: int(max_tokens)], ranked, skipped)


def evaluate_watchlist(
    engine: Any,
    symbols: Sequence[str],
    candles_by_symbol: Mapping[str, Sequence[Candle]],
    portfolio: Portfolio,
    *,
    auto_config: Optional[AutoConfig] = None,
    manual_strategy: Optional[ManualStrategy] = None,
    scores: Optional[Mapping[str, float]] = None,
    market_contexts: Optional[Mapping[str, Optional[Mapping[str, Any]]]] = None,
) -> list[Signal]:
    """One signal per watched token, ordered SELL first, then BUY by score (best first), then STAY_OUT.

    Exactly one of ``auto_config`` / ``manual_strategy`` is used as the template; its ``symbol`` is replaced per
    token. ``scores`` (e.g. from ``rank_tokens``) orders the BUYs; tokens without a score order by watchlist
    position. ``market_contexts`` is forwarded to ``engine.set_market_context`` per token when given.
    """
    if (auto_config is None) == (manual_strategy is None):
        raise ValueError("pass exactly one of auto_config or manual_strategy")
    syms = list(dict.fromkeys(s for s in symbols if s))[:MAX_WATCHLIST]
    scores = scores or {}
    out: list[tuple[int, float, int, Signal]] = []
    for idx, sym in enumerate(syms):
        if market_contexts is not None and hasattr(engine, "set_market_context"):
            engine.set_market_context(sym, market_contexts.get(sym))
        cs = candles_by_symbol.get(sym) or []
        try:
            if auto_config is not None:
                sig = engine.evaluate_auto(auto_config.model_copy(update={"symbol": sym}), cs, portfolio)
            else:
                assert manual_strategy is not None
                sig = engine.evaluate_manual(manual_strategy.model_copy(update={"symbol": sym}), cs, portfolio)
        except Exception as exc:  # one bad token must not stop the others
            sig = Signal(
                symbol=sym,
                action=Action.STAY_OUT,
                source=AgentMode.AUTO if auto_config is not None else AgentMode.MANUAL,
                strategy="NONE",
                reason=f"Could not evaluate {sym}: {exc}",
                conditions_failed=["evaluation_error"],
            )
        rank = {Action.SELL: 0, Action.BUY: 1}.get(sig.action, 2)
        out.append((rank, -float(scores.get(sym, 0.0)), idx, sig))
    out.sort(key=lambda r: r[:3])
    return [r[3] for r in out]


__all__ = [
    "ANY",
    "NARRATIVES",
    "NARRATIVE_LABELS",
    "MAX_WATCHLIST",
    "AutoPickConfig",
    "TokenPick",
    "RankResult",
    "normalize_narrative",
    "score_token",
    "rank_tokens",
    "evaluate_watchlist",
]
