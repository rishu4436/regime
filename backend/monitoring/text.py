"""One-prefix rule for human-readable reasons: the loop / alerts add exactly one prefix, whatever the producer
(risk layer, engine, older or newer module versions) already put in the text."""
from __future__ import annotations

import re


def _norm(s: str) -> str:
    return re.sub(r"[\s_]+", " ", s).strip().lower()


def one_prefix(prefix: str, text: str | None, default: str = "", keep_sentence: bool = True) -> str:
    """`prefix: text` with any existing copies of `prefix` removed.

    one_prefix("Paused", "lunch")                    -> "Paused: lunch"
    one_prefix("Paused", "Paused by user")           -> "Paused by user"      (already a sentence about it)
    one_prefix("Paused", "Paused: Paused: lunch")    -> "Paused: lunch"
    one_prefix("Emergency stop", "EMERGENCY STOP: x")-> "Emergency stop: x"
    one_prefix("RSI_EXIT", "RSI_EXIT: RSI(14)=83.8 > 70") -> "RSI_EXIT: RSI(14)=83.8 > 70"
    keep_sentence=False always shows the prefix (used for exit codes: "TAKE_PROFIT: take profit hit").
    """
    body = (text or "").strip() or default
    p = _norm(prefix)
    sentence = False
    while True:
        n = _norm(body)
        if not n.startswith(p):
            break
        # length of the prefix in the ORIGINAL text (same words, maybe '_' / case / spacing differences)
        m = re.match(r"\s*" + r"[\s_]+".join(re.escape(w) for w in p.split(" ")), body, flags=re.IGNORECASE)
        if not m:
            break
        rest = body[m.end():]
        if rest.lstrip().startswith(":") or rest.lstrip().startswith("-") or not rest.strip():
            body = rest.lstrip().lstrip(":-").strip()
            continue
        if rest[:1].isspace() or rest[:1] in "(,;":
            sentence = True  # e.g. "Paused by user", "Emergency stop by user"
        break
    if sentence and keep_sentence:
        return body
    return f"{prefix}: {body}" if body else prefix
