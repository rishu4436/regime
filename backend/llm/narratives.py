"""Narrative (sector) tags for Auto-pick, and plain-English wording for its picks.

``narrative_of(symbol, name=None)`` maps a tokenized stock to one of ``AI_CHIPS``, ``EV_ENERGY``, ``BIG_TECH``,
``CRYPTO`` or None (broad ETFs and anything unknown only match "Any"). It is map-only: no network, ever.

``make_narrative_of(explainer_or_client)`` returns the same lookup plus an optional Grok fallback for tokens the
map doesn't know. Grok is asked only when an API key is configured, with a short timeout, the answer must be exactly
one allowed code, and every answer is cached per symbol.

``explain_picks(picks, skipped)`` turns the engine's ``rank_tokens`` output into one sentence per token.
"""
from __future__ import annotations

import dataclasses
import logging
import re
import threading
from typing import Any, Callable, Mapping, Optional, Sequence

from . import live as live_facts

log = logging.getLogger(__name__)

AI_CHIPS, EV_ENERGY, BIG_TECH, CRYPTO, ANY = "AI_CHIPS", "EV_ENERGY", "BIG_TECH", "CRYPTO", "ANY"
NARRATIVES = (AI_CHIPS, EV_ENERGY, BIG_TECH, CRYPTO)
NARRATIVE_LABELS = {
    AI_CHIPS: "AI & chips",
    EV_ENERGY: "EV & energy",
    BIG_TECH: "Big Tech",
    CRYPTO: "Crypto-linked",
    ANY: "Any",
}

#: Underlying US ticker -> narrative. Kept small and factual; anything else is None (matches "Any" only).
SECTOR_MAP: dict[str, str] = {
    **dict.fromkeys(("NVDA", "AMD", "AVGO", "TSM", "INTC", "ARM", "MU", "QCOM", "ASML", "SMCI", "MRVL", "PLTR"), AI_CHIPS),
    **dict.fromkeys(("TSLA", "RIVN", "LCID", "NIO", "ENPH", "FSLR", "NEE", "XOM", "CVX"), EV_ENERGY),
    **dict.fromkeys(("AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "META", "NFLX", "ORCL", "CRM", "ADBE"), BIG_TECH),
    **dict.fromkeys(("COIN", "MSTR", "HOOD", "MARA", "RIOT", "CRCL", "GLXY"), CRYPTO),
}
#: Broad index ETFs: known, deliberately no narrative (and never sent to Grok).
BROAD_ETFS = frozenset({"SPY", "QQQ", "IVV", "VOO"})

#: Company-name hints (word match, lower case) used when the symbol isn't recognised.
NAME_HINTS: tuple[tuple[str, str], ...] = (
    ("nvidia", AI_CHIPS), ("advanced micro devices", AI_CHIPS), ("broadcom", AI_CHIPS),
    ("taiwan semiconductor", AI_CHIPS), ("intel", AI_CHIPS), ("arm holdings", AI_CHIPS), ("micron", AI_CHIPS),
    ("qualcomm", AI_CHIPS), ("asml", AI_CHIPS), ("super micro", AI_CHIPS), ("marvell", AI_CHIPS), ("palantir", AI_CHIPS),
    ("tesla", EV_ENERGY), ("rivian", EV_ENERGY), ("lucid", EV_ENERGY), ("nio", EV_ENERGY), ("enphase", EV_ENERGY),
    ("first solar", EV_ENERGY), ("nextera", EV_ENERGY), ("exxon", EV_ENERGY), ("chevron", EV_ENERGY),
    ("apple", BIG_TECH), ("microsoft", BIG_TECH), ("alphabet", BIG_TECH), ("google", BIG_TECH), ("amazon", BIG_TECH),
    ("meta platforms", BIG_TECH), ("netflix", BIG_TECH), ("oracle", BIG_TECH), ("salesforce", BIG_TECH),
    ("adobe", BIG_TECH),
    ("coinbase", CRYPTO), ("microstrategy", CRYPTO), ("robinhood", CRYPTO), ("marathon digital", CRYPTO),
    ("mara holdings", CRYPTO), ("riot platforms", CRYPTO), ("circle internet", CRYPTO), ("galaxy digital", CRYPTO),
)
_NAME_RES = tuple((re.compile(rf"\b{re.escape(k)}\b"), v) for k, v in NAME_HINTS)
_ETF_NAME_RE = re.compile(r"\b(s&p 500|nasdaq[- ]100|etf|index fund|spdr|vanguard|ishares)\b", re.I)

# Tokenized-stock suffixes: Ondo "on" (NVDAon), Binance bStocks "B" (NVDAB), Backed xStocks "x" (TSLAx).
_SUFFIXES = ("ON", "B", "X")


def underlying_ticker(symbol: str) -> Optional[str]:
    """'NVDAB' / 'NVDAon' / 'NVDAx' / 'NVDA' -> 'NVDA'. A suffix is stripped only when what's left is a ticker we
    know (sector map or broad ETF), so a real ticker that merely ends in B or X is never cut short. None if unknown."""
    s = re.sub(r"[^A-Za-z0-9]", "", str(symbol or "")).upper()
    if not s:
        return None
    known = SECTOR_MAP.keys() | BROAD_ETFS
    if s in known:
        return s
    for suf in _SUFFIXES:
        if s.endswith(suf) and s[: -len(suf)] in known:
            return s[: -len(suf)]
    return None


def _from_name(name: Optional[str]) -> Optional[str]:
    n = str(name or "").lower()
    if not n or _ETF_NAME_RE.search(n):
        return None
    for rx, code in _NAME_RES:
        if rx.search(n):
            return code
    return None


def _lookup(symbol: str, name: Optional[str]) -> tuple[bool, Optional[str]]:
    """(known, narrative). known=True means the map decided (including 'broad ETF -> None')."""
    t = underlying_ticker(symbol)
    if t is not None:
        return True, SECTOR_MAP.get(t)
    hint = _from_name(name)
    if hint is not None:
        return True, hint
    if name and _ETF_NAME_RE.search(str(name)):
        return True, None
    return False, None


def narrative_of(symbol: str, name: Optional[str] = None) -> Optional[str]:
    """Map-only narrative for a token: AI_CHIPS / EV_ENERGY / BIG_TECH / CRYPTO, or None. Never uses the network."""
    return _lookup(symbol, name)[1]


# ------------------------------------------------------------------ optional Grok fallback

GROK_TIMEOUT_S = 6.0
_GROK_SYSTEM = (
    "You classify a tokenized US stock into a sector narrative. Reply with exactly one code and nothing else: "
    "AI_CHIPS (AI, semiconductors, AI software), EV_ENERGY (electric vehicles, clean or traditional energy), "
    "BIG_TECH (large consumer / enterprise tech platforms), CRYPTO (companies whose business is tied to crypto), "
    "or NONE (anything else, index funds, or if unsure)."
)


def parse_narrative_code(text: Any) -> Optional[str]:
    """Accept only an exact code (quotes / trailing period / case tolerated). Anything else -> None."""
    s = re.sub(r"^[\s\"'`*]+|[\s\"'`*.]+$", "", str(text or "")).upper()
    return s if s in NARRATIVES else None


def _short_timeout_client(client: Any, timeout_s: float) -> Any:
    from .client import GrokClient

    if isinstance(client, GrokClient):
        cfg = dataclasses.replace(client.config, timeout_s=min(float(client.config.timeout_s), timeout_s))
        return GrokClient(config=cfg, http=client.http)
    return client


def make_narrative_of(explainer_or_client: Any = None, *, timeout_s: float = GROK_TIMEOUT_S,
                      use_llm: Optional[bool] = None) -> Callable[..., Optional[str]]:
    """``narrative_of``-compatible callable ``(symbol, name=None)`` with a Grok fallback for unmapped tokens.

    ``explainer_or_client``: a DefaultExplainer (its ``.client`` is used), a GrokClient (or anything with ``chat()``
    and ``enabled``), or None to read XAI_API_KEY from the environment. No key -> map only, no network.
    """
    src = explainer_or_client
    client = getattr(src, "client", src) if src is not None else None
    if client is None:
        from .client import GrokClient, GrokConfig

        client = GrokClient(GrokConfig.from_env())
    enabled = bool(getattr(client, "enabled", False))
    if use_llm is None and src is not None and hasattr(src, "use_llm"):
        use_llm = bool(src.use_llm)
    llm_on = enabled and (use_llm is None or use_llm)
    chat_client = _short_timeout_client(client, timeout_s) if llm_on else None
    cache: dict[str, Optional[str]] = {}
    lock = threading.Lock()

    def lookup(symbol: str, name: Optional[str] = None) -> Optional[str]:
        known, code = _lookup(symbol, name)
        if known or not llm_on:
            return code
        key = re.sub(r"[^A-Za-z0-9]", "", str(symbol or "")).upper()
        if not key:
            return None
        with lock:
            if key in cache:
                return cache[key]
            user = f"Token symbol: {str(symbol)[:40]}" + (f"\nToken name: {str(name)[:120]}" if name else "")
            try:
                ans = chat_client.chat([{"role": "system", "content": _GROK_SYSTEM}, {"role": "user", "content": user}],
                                       temperature=0.0, max_tokens=10)
                code = parse_narrative_code(ans)
            except Exception as e:  # noqa: BLE001 - a failed lookup only means "no narrative"
                log.info("narrative lookup for %s failed: %s", key, type(e).__name__)
                code = None
            cache[key] = code
            return code

    lookup.cache = cache  # type: ignore[attr-defined]  # for tests / diagnostics
    return lookup


# ------------------------------------------------------------------ Auto-pick wording

_REGIME_WORDS = {
    "STRONG_UPTREND": "strong uptrend", "MILD_UPTREND": "mild uptrend", "SIDEWAYS": "sideways",
    "OVERSOLD_PULLBACK": "oversold pullback", "HIGH_VOLATILITY": "high volatility", "DOWNTREND": "downtrend",
}
_RAW_CODE_RE = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")
_BARE_CODE_RE = re.compile(r"\b(?:SIDEWAYS|DOWNTREND)\b")  # regime codes without an underscore


def narrative_label(code: Any) -> Optional[str]:
    """'AI_CHIPS' / 'AI & chips' -> 'AI & chips'; ANY / None / unknown -> None."""
    s = str(code or "").strip()
    if not s:
        return None
    for k, v in NARRATIVE_LABELS.items():
        if s.lower() in (k.lower(), v.lower()):
            return None if k == ANY else v
    return s.replace("_", " ").capitalize() if _RAW_CODE_RE.fullmatch(s) else s


def _no_codes(text: str) -> str:
    def repl(m: re.Match) -> str:
        w = m.group(0)
        if w in _REGIME_WORDS:
            return _REGIME_WORDS[w]
        if w in NARRATIVE_LABELS:
            return NARRATIVE_LABELS[w]
        return w.replace("_", " ").lower()

    text = _RAW_CODE_RE.sub(repl, text)
    return _BARE_CODE_RE.sub(lambda m: _REGIME_WORDS[m.group(0)], text)


def _get(obj: Any, key: str) -> Any:
    return obj.get(key) if isinstance(obj, Mapping) else getattr(obj, key, None)


def _sentence(parts: Sequence[str]) -> str:
    body = ", ".join(p.strip() for p in parts if p and p.strip())
    return body.rstrip(".")


def explain_pick(pick: Any) -> str:
    """'Picked NVDAB (AI & chips): up 6.2% over 20 bars, above a rising 50-bar average, 24h volume 1.2M USDT,
    market strong uptrend.'"""
    sym = str(_get(pick, "symbol") or "?")
    label = narrative_label(_get(pick, "narrative"))
    labels = {v.lower() for v in NARRATIVE_LABELS.values()} | {k.lower() for k in NARRATIVE_LABELS}
    parts = [p for p in re.split(r"\s*;\s*", str(_get(pick, "reason") or "")) if p.strip()]
    parts = [p for p in parts if p.strip().lower() not in labels and (label is None or p.strip().lower() != label.lower())]
    head = f"Picked {sym}" + (f" ({label})" if label else "")
    body = _no_codes(_sentence(parts))
    return f"{head}: {body}." if body else f"{head}."


def explain_skip(item: Any) -> str:
    """'Skipped TSLAx: xStock tokens have almost no liquidity on BNB Chain.'"""
    sym = str(_get(item, "symbol") or "?")
    reason = str(_get(item, "reason") or "").strip()
    text = live_facts.plain_reason(reason, keywords=False) if reason else "no reason given"
    return f"Skipped {sym}: {_no_codes(text).rstrip('.')}."


def explain_picks(picks: Sequence[Any], skipped: Optional[Sequence[Any]] = None) -> list[str]:
    """One plain sentence per picked token, then one per skipped token. Accepts dicts or the engine's TokenPick
    objects, or a RankResult-like object as ``picks`` (its ``picks`` / ``skipped`` are used)."""
    if not isinstance(picks, (list, tuple)) and hasattr(picks, "picks"):
        skipped = skipped if skipped is not None else getattr(picks, "skipped", None)
        picks = picks.picks
    return [explain_pick(p) for p in (picks or [])] + [explain_skip(s) for s in (skipped or [])]


__all__ = [
    "AI_CHIPS", "EV_ENERGY", "BIG_TECH", "CRYPTO", "ANY", "NARRATIVES", "NARRATIVE_LABELS", "SECTOR_MAP", "BROAD_ETFS",
    "underlying_ticker", "narrative_of", "make_narrative_of", "parse_narrative_code", "narrative_label",
    "explain_pick", "explain_skip", "explain_picks",
]
