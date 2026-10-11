"""Natural language -> ManualStrategy | AutoConfig.

Two pieces:
- `keyword_parse`: deterministic regex/keyword parser (the offline fallback).
- `validate_llm_strategy`: turns raw LLM text into a contract-valid model (extract JSON, prune unknown keys,
  drop invalid fields, apply safety overrides) or raises `StrategyValidationError`.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional, Union

from pydantic import BaseModel, ValidationError

from backend.contracts import (
    DEFAULT_INITIAL_CAPITAL,
    ActivationMode,
    AutoConfig,
    BreakoutDirection,
    ConditionLogic,
    ManualStrategy,
    PresetName,
    Timeframe,
    TraderStyle,
)

ParsedStrategy = Union[ManualStrategy, AutoConfig]

KNOWN_SYMBOLS = ["NVDAon", "TSLAx", "AAPLB", "SPYon", "METAx"]
_ALIASES: list[tuple[str, str]] = [
    (r"nvidia|nvda", "NVDAon"),
    (r"tesla|tsla", "TSLAx"),
    (r"apple|aapl", "AAPLB"),
    (r"s&p\s*500|s&p|spy", "SPYon"),
    (r"meta|facebook", "METAx"),
]
_FALLBACK_SYMBOL = "NVDAon"
# Team default entry when the text gives no entry rule (Bot 1 / Bot 2): price above EMA(20). The contract default
# (RSI < 35 AND close > SMA50) practically never triggers, so it is not used for parsed strategies.
DEFAULT_ENTRY_EMA_PERIOD = 20
DEFAULT_ENTRY_NOTE = ("No entry rule found in your text, so the entry is set to the team default: buy when price is "
                      f"above EMA({DEFAULT_ENTRY_EMA_PERIOD}).")
_ENTRY_NAMES = ("rsi_below", "price_above_sma", "price_above_ema", "macd_bullish_cross", "bollinger_lower",
                "volume_above_avg", "breakout")
_ABOVE = r"(?:>|above|over|greater than|rises above|crosses above|goes above|moves above|climbs above|gets above|goes over)"
_BELOW = r"(?:<|below|under|less than|drops below|falls below|goes below|dips below|moves below|gets below|drops under)"


# Volume entry: 'volume spike', 'volume is above (its) average', 'volume goes above average', 'volume is high',
# 'volume higher than average', 'high volume', 'above-average volume', 'volume is running above average', ...
_VOLUME_ENTRY_RE = re.compile(
    r"volume\s+(?:is\s+(?:running\s+|getting\s+)?|goes\s+|gets\s+|rises\s+|climbs\s+|moves\s+|jumps\s+|spikes\s+|runs\s+|stays\s+|are\s+)?"
    r"(?:spik|surg|above|over|>|higher|greater|bigger|larger|high\b|elevated|strong|heavy|rising|increas|picks? up|\d+(?:\.\d+)?\s*x)"
    r"|(?:high|heavy|strong|elevated|rising|increasing|big|large|above[\s-]+(?:the\s+)?(?:avg|average))\s+volume"
    r"|volume\s+(?:spike|surge|breakout)", re.I)
RSI_EXIT_NOTE = ("RSI exit at {thr:g} is on by default (sells when RSI goes above {thr:g}); turn it off in the form if you "
                 "only want take-profit and stop-loss.")
# indicator word in the entry text -> the entry condition it should have produced
_INDICATOR_WORDS: list[tuple[str, str]] = [
    (r"\bvolume\b", "volume_above_avg"), (r"\bmacd\b", "macd_bullish_cross"), (r"\brsi\b|oversold", "rsi_below"),
    (r"bollinger|lower band", "bollinger_lower"), (r"\bema\b|exponential moving average", "price_above_ema"),
    (r"\bsma\b|simple moving average|(?<!exponential )moving average", "price_above_sma"), (r"breakout|breaks?\s", "breakout"),
]
_RSI_EXIT_MENTION_RE = re.compile(r"rsi\s*exit|(?:sell|exit|close|get out|take profit)[^.;]*\brsi\b|\brsi\b[^.;]*\b(?:sell|exit)", re.I)


def unparsed_notes(text: str, enabled: set[str]) -> list[str]:
    """'Couldn't understand "<phrase>"; it was left out' for indicator words in the entry part that produced no
    entry condition (best effort: nothing is silently dropped)."""
    t = re.sub(r"(?:no|without (?:an? )?)\s*rsi\s*exit|rsi\s*(?:\(\s*\d+\s*\)\s*)?period\s*(?:of\s*)?\d+", " ", (text or "").lower())
    entry_part = t
    for chunk in re.findall(r"(?:sell|exit|close|get out)[^.;]*", t):
        entry_part = entry_part.replace(chunk, " ")
    cond_part = re.split(r"take[\s-]?profit|stop|trailing|position size|\bsize\b|cool[\s-]?down|\bmax|min(?:imum)?\s*volume|"
                         r"require|filter|market hours", entry_part)[0]
    pieces = [p.strip(" ,.;") for p in re.split(r",|;|\band\b|\bor\b|\bthen\b", cond_part) if p.strip(" ,.;")]
    notes: list[str] = []
    for word, cond in _INDICATOR_WORDS:
        if cond in enabled:
            continue
        for piece in pieces:
            if re.search(word, piece):
                phrase = re.sub(r"^(?:buy|enter|go long|long)\s+(?:when|if|on)?\s*", "", piece).strip() or piece
                note = f'Couldn\'t understand "{phrase}"; it was left out.'
                if note not in notes:
                    notes.append(note)
                break
    return notes


def strategy_notes(text: str, strategy: ManualStrategy, default_entry_used: bool) -> list[str]:
    """User-facing notes for a parsed manual strategy (keyword or Grok path)."""
    notes: list[str] = [DEFAULT_ENTRY_NOTE] if default_entry_used else []
    enabled = {n for n in _ENTRY_NAMES if getattr(strategy.entry, n).enabled}
    if not default_entry_used:
        notes += unparsed_notes(text, enabled)
    if strategy.exit.rsi_above.enabled and not _RSI_EXIT_MENTION_RE.search(text or ""):
        notes.append(RSI_EXIT_NOTE.format(thr=strategy.exit.rsi_above.threshold))
    return notes


class StrategyValidationError(ValueError):
    pass


# --------------------------------------------------------------------------- helpers
_NUM = r"(\d+(?:\.\d+)?)"


def _num(pattern: str, text: str) -> Optional[float]:
    m = re.search(pattern, text, re.I)
    return float(m.group(1)) if m else None


def _int(pattern: str, text: str) -> Optional[int]:
    v = _num(pattern, text)
    return int(v) if v is not None else None


def detect_symbol(text: str, default_symbol: Optional[str] = None) -> str:
    for sym in KNOWN_SYMBOLS:
        if re.search(rf"(?<![A-Za-z0-9]){re.escape(sym)}(?![A-Za-z0-9])", text, re.I):
            return sym
    # other tokenized-stock-looking symbols, e.g. AMZNon / GOOGLx / MSFTB
    m = re.search(r"(?<![A-Za-z0-9])([A-Z]{2,5}(?:on|x|B))(?![A-Za-z0-9])", text)
    if m:
        return m.group(1)
    for pat, sym in _ALIASES:
        if re.search(rf"(?<![A-Za-z0-9])(?:{pat})(?![A-Za-z0-9])", text, re.I):
            return sym
    return default_symbol or _FALLBACK_SYMBOL


def detect_timeframe(text: str) -> Optional[Timeframe]:
    t = text.lower()
    table = [
        (r"\b(1\s*m|1\s*-?\s*min(ute)?)\b", Timeframe.M1), (r"\b(5\s*m|5\s*-?\s*min(ute)?s?)\b", Timeframe.M5),
        (r"\b(15\s*m|15\s*-?\s*min(ute)?s?)\b", Timeframe.M15), (r"\b(4\s*h|4\s*-?\s*hours?)\b", Timeframe.H4),
        (r"\b(1\s*h|1\s*-?\s*hour|hourly)\b", Timeframe.H1), (r"\b(1\s*d|daily|1\s*-?\s*day)\b", Timeframe.D1),
    ]
    for pat, tf in table:
        if re.search(pat, t):
            return tf
    return None


def wants_auto_execute(text: str) -> bool:
    """Only an explicit request switches activation away from the safe CONFIRMATION default."""
    t = text.lower()
    if re.search(r"(ask|confirm|confirmation|approve|approval)", t) and not re.search(r"without (asking|confirm)", t):
        return False
    return bool(re.search(r"auto[\s-]?execut|execute automatically|without (asking|confirm)|no confirmation|fully automatic", t))


_AUTO_HINTS = re.compile(
    r"\bauto(\s|-)?(mode|pilot|agent)\b|\blet (the )?(agent|bot|ai) (decide|choose|pick)|\b(patient|accumulat|balanced|swing trader|"
    r"scalp|active trader|aggressive|conservative)\w*|\btrader style\b|\b(smart\s*dca|dca|grid|buy the dip|trend[\s-]?following|presets?)\b",
    re.I,
)
_MANUAL_HINTS = re.compile(r"\b(rsi|sma|ema|macd|bollinger|moving average|take[\s-]?profit|stop[\s-]?loss|trailing|breakout|cooldown)\b", re.I)


def looks_like_auto(text: str) -> bool:
    if re.search(r"\bmanual\b", text, re.I):
        return False
    auto = len(_AUTO_HINTS.findall(text))
    manual = len(_MANUAL_HINTS.findall(text))
    return auto > 0 and auto >= manual


# --------------------------------------------------------------------------- keyword parser
def _parse_auto(text: str, default_symbol: Optional[str]) -> AutoConfig:
    t = text.lower()
    style = TraderStyle.BALANCED
    if re.search(r"patient|accumulat|long[\s-]?term|conservative|passive", t):
        style = TraderStyle.PATIENT
    elif re.search(r"active|scalp|aggressive|day[\s-]?trad|short[\s-]?term", t):
        style = TraderStyle.ACTIVE
    presets: list[PresetName] = []
    if re.search(r"smart\s*dca", t):
        presets.append(PresetName.SMART_DCA)
    if re.search(r"(?<!smart )(?<!smart)\bdca\b|dollar[\s-]?cost", t) and PresetName.DCA not in presets:
        presets.append(PresetName.DCA)
    if re.search(r"\bdip", t):
        presets.append(PresetName.BUY_THE_DIP)
    if re.search(r"\btrend|momentum", t):
        presets.append(PresetName.TREND_FOLLOWING)
    if re.search(r"\bgrid", t):
        presets.append(PresetName.GRID)
    capital = _capital(t)
    kwargs: dict[str, Any] = dict(symbol=detect_symbol(text, default_symbol), style=style, capital=capital or DEFAULT_INITIAL_CAPITAL)
    if presets:
        kwargs["enabled_presets"] = presets
    tf = detect_timeframe(text)
    if tf:
        kwargs["timeframe"] = tf
    if wants_auto_execute(text):
        kwargs["activation"] = ActivationMode.AUTO_EXECUTE
    return AutoConfig(**kwargs)


def _capital(t: str) -> Optional[float]:
    m = re.search(r"\$\s*(\d[\d,]*(?:\.\d+)?)\s*(k)?\b", t) or re.search(r"(\d[\d,]*(?:\.\d+)?)\s*(k)?\s*(?:usdt|usd|dollars|bucks)\b", t) \
        or re.search(r"capital\s*(?:of|=|:)?\s*\$?\s*(\d[\d,]*(?:\.\d+)?)\s*(k)?\b", t)
    if not m:
        return None
    v = float(m.group(1).replace(",", ""))
    if m.group(2):
        v *= 1000
    return v if v > 0 else None


def _parse_manual(text: str, default_symbol: Optional[str]) -> ManualStrategy:
    return parse_manual_with_notes(text, default_symbol)[0]


def default_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Entry dict with only the team default enabled: price above EMA(20)."""
    for name in _ENTRY_NAMES:
        entry[name]["enabled"] = False
    entry["price_above_ema"].update(enabled=True, period=DEFAULT_ENTRY_EMA_PERIOD)
    entry["logic"] = ConditionLogic.AND.value
    return entry


def parse_manual_with_notes(text: str, default_symbol: Optional[str]) -> tuple[ManualStrategy, list[str]]:
    notes: list[str] = []
    t = text.lower()
    s = ManualStrategy(symbol=detect_symbol(text, default_symbol)).model_dump()
    e, x, r, f = s["entry"], s["exit"], s["risk"], s["filters"]

    # split entry vs exit phrases so "sell when RSI above 70" doesn't turn into an entry condition
    exit_part = " ".join(re.findall(r"(?:sell|exit|close|take profit|get out)[^.;]*", t))
    entry_part = t
    for chunk in re.findall(r"(?:sell|exit|close|get out)[^.;]*", t):
        entry_part = entry_part.replace(chunk, " ")

    mentioned: set[str] = set()
    rsi_below = _num(r"rsi\s*(?:\(\s*\d+\s*\))?\s*(?:is\s*)?" + _BELOW + r"\s*" + _NUM, entry_part)
    if rsi_below is None and re.search(r"oversold", entry_part):
        rsi_below = 30.0
    if rsi_below is not None:
        e["rsi_below"].update(enabled=True, threshold=min(rsi_below, 100.0)); mentioned.add("rsi_below")
    rp = _int(r"rsi\s*\(?\s*(\d+)\s*\)?\s*(?:period)?\s*(?:" + _BELOW + "|" + _ABOVE + ")", t) or _int(r"rsi\s*period\s*(?:of\s*)?(\d+)", t)
    if rp and rp >= 2:
        e["rsi_below"]["period"] = rp; x["rsi_above"]["period"] = rp

    sma = re.search(r"(?:above|over)\s*(?:the\s*)?(?:(\d+)[\s-]*(?:period|day|bar)?\s*)?(?:sma|simple moving average|moving average|ma)\b(?:\s*\(?\s*(\d+))?", entry_part) \
        or re.search(r"\b(?:sma|ma)\s*\(?\s*(\d+)\)?", entry_part)
    if sma and not re.search(r"\bema\b", sma.group(0)):
        per = next((g for g in sma.groups() if g), None)
        e["price_above_sma"].update(enabled=True, **({"period": int(per)} if per and int(per) >= 2 else {})); mentioned.add("price_above_sma")
    ema = re.search(r"(?:above|over)\s*(?:the\s*)?(?:(\d+)[\s-]*(?:period|day|bar)?\s*)?(?:ema|exponential moving average)\b(?:\s*\(?\s*(\d+))?", entry_part) \
        or re.search(r"\bema\s*\(?\s*(\d+)\)?", entry_part)
    if ema:
        per = next((g for g in ema.groups() if g), None)
        e["price_above_ema"].update(enabled=True, **({"period": int(per)} if per and int(per) >= 2 else {})); mentioned.add("price_above_ema")
    if re.search(r"macd", entry_part) and not re.search(r"bearish", entry_part):
        e["macd_bullish_cross"]["enabled"] = True; mentioned.add("macd_bullish_cross")
    if re.search(r"bollinger|lower band|\bbb\b", entry_part):
        e["bollinger_lower"]["enabled"] = True; mentioned.add("bollinger_lower")
    if _VOLUME_ENTRY_RE.search(entry_part) and not re.search(r"min(imum)?\s*volume|require", entry_part):
        e["volume_above_avg"]["enabled"] = True; mentioned.add("volume_above_avg")
        m = _num(r"volume[^.;]*?" + _NUM + r"\s*x", entry_part)
        if m and m > 0:
            e["volume_above_avg"]["multiplier"] = m
    if re.search(r"breakout|breaks? (?:out|above|below)|break of", entry_part):
        e["breakout"]["enabled"] = True; mentioned.add("breakout")
        if re.search(r"(?:breaks?|breakout)\s*(?:below|down|of (?:the )?(?:recent |\d+[\s-]*bar )?low)|\blow\b", entry_part):
            e["breakout"]["direction"] = BreakoutDirection.LOW.value
        lb = _int(r"(\d+)[\s-]*(?:bar|candle|period|day)s?\s*(?:high|low)", entry_part)
        if lb and lb >= 2:
            e["breakout"]["lookback"] = lb
    if mentioned:  # user listed their own entry conditions -> only those are enabled
        for name in _ENTRY_NAMES:
            if name not in mentioned:
                e[name]["enabled"] = False
    else:  # no entry rule in the text -> team default (price above EMA 20), shown to the user via notes
        default_entry(e)
        notes.append(DEFAULT_ENTRY_NOTE)
    # the entry conditions themselves (stop before exit / sizing phrases) decide a plain "or"
    cond_part = re.split(r"take[\s-]?profit|stop|trailing|position size|\bsize\b|cool[\s-]?down|\bmax", entry_part)[0]
    if re.search(r"\b(any of|either|or logic|any one)\b|\bany\s+(?:condition|signal)", t):
        e["logic"] = ConditionLogic.OR.value
    elif re.search(r"\b(all of|and logic|all conditions|both)\b", t):
        e["logic"] = ConditionLogic.AND.value
    elif len(mentioned) >= 2 and re.search(r"\bor\b", cond_part) and not re.search(r"\band\b", cond_part):
        e["logic"] = ConditionLogic.OR.value

    tp = _num(r"(?:take[\s-]?profit|\btp\b|profit target|target)\s*(?:at|of|=|:)?\s*\+?" + _NUM, t) or _num(_NUM + r"\s*%\s*(?:take[\s-]?profit|profit|gain)", t)
    if tp and tp > 0:
        x["take_profit_pct"] = tp
    # remove trailing-stop phrases first so "trailing stop 2.5%, stop loss 5%" keeps SL = 5 (P1)
    t_sl = re.sub(r"trailing[\s-]*stop(?:[\s-]*loss)?\s*(?:at|of|=|:)?\s*-?" + _NUM + r"\s*%?|" + _NUM
                  + r"\s*%\s*trailing[\s-]*(?:stop(?:[\s-]*loss)?)?|trailing[\s-]*stop(?:[\s-]*loss)?", " ", t)
    sl = _num(r"(?:stop[\s-]?loss|\bsl\b|\bstop\b)\s*(?:at|of|=|:)?\s*-?" + _NUM, t_sl) or _num(_NUM + r"\s*%\s*(?:stop[\s-]?loss|stop)", t_sl)
    if sl and sl > 0:
        x["stop_loss_pct"] = sl
    trail = _num(r"trailing\s*(?:stop)?\s*(?:loss)?\s*(?:at|of|=|:)?\s*" + _NUM, t) or _num(_NUM + r"\s*%\s*trailing", t)
    if trail and trail > 0:
        x["trailing_stop"].update(enabled=True, pct=trail)
    elif re.search(r"trailing", t):
        x["trailing_stop"]["enabled"] = True
    rsi_exit = _num(r"rsi\s*(?:\(\s*\d+\s*\))?\s*(?:is\s*)?" + _ABOVE + r"\s*" + _NUM, exit_part or t)
    if rsi_exit is not None and (exit_part or rsi_below is not None):
        x["rsi_above"].update(enabled=True, threshold=min(rsi_exit, 100.0))
    if re.search(r"no rsi exit|without (?:an )?rsi exit", t):
        x["rsi_above"]["enabled"] = False
    if re.search(r"macd", exit_part) or re.search(r"bearish (?:macd )?cross|macd (?:bearish|exit)", t):
        x["macd_bearish_cross"]["enabled"] = True

    size = _num(r"(?:position size|size|allocate|risk|invest|use)\s*(?:of|=|:)?\s*" + _NUM + r"\s*%", t) or _num(_NUM + r"\s*%\s*(?:of (?:my |the )?(?:capital|portfolio|equity|account)|per (?:trade|position|entry)|position)", t)
    if size and 0 < size <= 100:
        r["position_size_pct"] = size
    mp = _int(r"(?:max(?:imum)?|up to|at most)\s*(\d+)\s*(?:open\s*)?positions?", t) or _int(r"max(?:imum)?\s*(?:open\s*)?positions?\s*(?:of|=|:)?\s*(\d+)", t)
    if mp and mp >= 1:
        r["max_open_positions"] = mp
    cd = _int(r"cool[\s-]?down\s*(?:of|=|:)?\s*(\d+)", t) or _int(r"(\d+)[\s-]*bars?\s*cool[\s-]?down", t)
    if cd is not None:
        r["cooldown_bars"] = cd
    ml = _num(r"max(?:imum)?\s*loss\s*(?:per trade)?\s*(?:of|=|:)?\s*" + _NUM, t)
    if ml and ml > 0:
        r["max_loss_per_trade_pct"] = ml

    if re.search(r"market hours|us session|trading hours|regular hours", t):
        f["market_hours"]["enabled"] = True
    if re.search(r"min(?:imum)?\s*volume|liquidity filter|require (?:above[\s-]average )?volume", t):
        f["min_volume"]["enabled"] = True
        mv = _num(r"min(?:imum)?\s*volume\s*(?:of|=|:)?\s*" + _NUM, t)
        if mv is not None:
            f["min_volume"]["min_volume"] = mv

    tf = detect_timeframe(text)
    if tf:
        s["timeframe"] = tf.value
    s["activation"] = (ActivationMode.AUTO_EXECUTE if wants_auto_execute(text) else ActivationMode.CONFIRMATION).value
    name = re.search(r"(?:call(?:ed)? it|name(?:d)?(?: it)?)\s*[\"']?([\w\s-]{1,40}?)[\"']?(?:[.,;]|$)", text, re.I)
    s["name"] = name.group(1).strip() if name else f"{s['symbol']} rule set"
    try:
        out = ManualStrategy.model_validate(s)
        return out, strategy_notes(text, out, bool(notes))
    except ValidationError:  # should be unreachable; never let the parser raise on odd input
        fb = ManualStrategy(symbol=s["symbol"]).model_dump()
        default_entry(fb["entry"])
        out = ManualStrategy.model_validate(fb)
        return out, strategy_notes(text, out, True)


def keyword_parse(text: str, default_symbol: Optional[str] = None) -> ParsedStrategy:
    """Deterministic fallback: never raises, always returns a contract-valid model."""
    return keyword_parse_with_notes(text, default_symbol)[0]


def keyword_parse_with_notes(text: str, default_symbol: Optional[str] = None) -> tuple[ParsedStrategy, list[str]]:
    """keyword_parse plus user-facing notes about defaults that were filled in (e.g. the default entry rule)."""
    text = text or ""
    if looks_like_auto(text):
        return _parse_auto(text, default_symbol), []
    return parse_manual_with_notes(text, default_symbol)


def text_has_entry_rule(text: str) -> bool:
    """True if the user's text names at least one entry condition (keyword parser's view)."""
    return DEFAULT_ENTRY_NOTE not in parse_manual_with_notes(text or "", None)[1]


# --------------------------------------------------------------------------- LLM output validation / repair
_SERVER_FIELDS = {"id", "created_at", "version"}


def extract_json(raw: str) -> Any:
    """Pull the first JSON object out of an LLM reply (handles ```json fences and surrounding prose)."""
    if not raw:
        raise StrategyValidationError("empty LLM output")
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.S)
    candidates = [fence.group(1)] if fence else []
    start = raw.find("{")
    if start >= 0:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(raw)):
            ch = raw[i]
            if in_str:
                esc = (ch == "\\") and not esc
                if ch == '"' and not esc:
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(raw[start:i + 1])
                    break
    for c in candidates:
        try:
            return json.loads(c)
        except json.JSONDecodeError:
            try:  # common repair: trailing commas
                return json.loads(re.sub(r",\s*([}\]])", r"\1", c))
            except json.JSONDecodeError:
                continue
    raise StrategyValidationError("no JSON object in LLM output")


def _prune(data: Any, model: type[BaseModel]) -> Any:
    """Recursively drop keys the contract model doesn't know (models are extra='forbid')."""
    if not isinstance(data, dict):
        return data
    out: dict[str, Any] = {}
    for key, value in data.items():
        fld = model.model_fields.get(key)
        if fld is None:
            continue
        ann = fld.annotation
        sub = ann if isinstance(ann, type) and issubclass(ann, BaseModel) else None
        out[key] = _prune(value, sub) if sub else _normalize_enum_case(value)
    return out


def _normalize_enum_case(v: Any) -> Any:
    if isinstance(v, str) and re.fullmatch(r"[a-z_ ]+", v.strip()):
        return v.strip().upper().replace(" ", "_")
    if isinstance(v, list):
        return [_normalize_enum_case(i) for i in v]
    return v


def _drop_path(data: dict[str, Any], loc: tuple[Any, ...]) -> bool:
    cur: Any = data
    for part in loc[:-1]:
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and isinstance(part, int) and part < len(cur):
            cur = cur[part]
        else:
            return False
    last = loc[-1] if loc else None
    if isinstance(cur, dict) and last in cur:
        del cur[last]
        return True
    if isinstance(cur, list) and isinstance(last, int) and last < len(cur):
        cur.pop(last)
        return True
    return False


def _validate_with_repair(data: dict[str, Any], model: type[BaseModel], max_rounds: int = 10) -> BaseModel:
    data = _prune(data, model)
    for _ in range(max_rounds):
        try:
            return model.model_validate(data)
        except ValidationError as err:
            dropped = False
            for e in err.errors():
                loc = tuple(e["loc"])
                # symbol/capital are required for AutoConfig; let the caller's defaults fill them
                dropped |= _drop_path(data, loc) or (len(loc) > 1 and _drop_path(data, loc[:-1]))
            if not dropped:
                raise StrategyValidationError(str(err)) from err
    raise StrategyValidationError("could not repair LLM output")


def _backfill_entry(result: ManualStrategy, text: str, default_symbol: Optional[str]) -> ManualStrategy:
    """Entry conditions the keyword parser clearly found in the text but the LLM left out (e.g. 'volume is above
    average') are switched on, with the keyword parser's parameters. Never removes what the LLM set."""
    kw = parse_manual_with_notes(text, default_symbol)[0]
    d = result.model_dump()
    added = False
    for name in _ENTRY_NAMES:
        if getattr(kw.entry, name).enabled and not d["entry"][name].get("enabled"):
            d["entry"][name] = getattr(kw.entry, name).model_dump()
            added = True
    if not added:
        return result
    try:
        return ManualStrategy.model_validate(d)
    except ValidationError:  # pragma: no cover - defensive
        return result


def validate_llm_strategy(raw: str, text: str, default_symbol: Optional[str] = None) -> ParsedStrategy:
    """Validate (and minimally repair) an LLM reply of the form {"kind": "manual"|"auto", "strategy": {...}}.

    Safety overrides applied regardless of what the model said:
    - activation is CONFIRMATION unless the user's own text explicitly asked for auto-execution;
    - server-owned fields (id, created_at, version) are regenerated;
    - a missing / empty symbol falls back to the symbol detected in the text (or default_symbol).
    """
    obj = extract_json(raw)
    if not isinstance(obj, dict):
        raise StrategyValidationError("LLM output is not a JSON object")
    kind = str(obj.get("kind") or obj.get("type") or "").strip().lower()
    body = obj.get("strategy") or obj.get("config") or {k: v for k, v in obj.items() if k not in {"kind", "type"}}
    if not isinstance(body, dict):
        raise StrategyValidationError("strategy body is not an object")
    if kind not in {"manual", "auto"}:
        kind = "auto" if ("style" in body or "enabled_presets" in body) else "manual"
    body = {k: v for k, v in body.items() if k not in _SERVER_FIELDS}
    model: type[BaseModel] = AutoConfig if kind == "auto" else ManualStrategy
    data = _prune(body, model)
    sym = data.get("symbol")
    if not isinstance(sym, str) or not sym.strip():
        data["symbol"] = detect_symbol(text, default_symbol)
    if kind == "auto":
        cap = data.get("capital")
        if not isinstance(cap, (int, float)) or cap <= 0:
            data["capital"] = _capital(text.lower()) or DEFAULT_INITIAL_CAPITAL
    data["activation"] = (ActivationMode.AUTO_EXECUTE if wants_auto_execute(text) else ActivationMode.CONFIRMATION).value
    result = _validate_with_repair(data, model)
    if kind == "manual" and isinstance(result, ManualStrategy) and not text_has_entry_rule(text):
        d = result.model_dump()
        default_entry(d["entry"])
        result = ManualStrategy.model_validate(d)
    elif kind == "manual" and isinstance(result, ManualStrategy):
        result = _backfill_entry(result, text, default_symbol)
    if kind == "auto" and not isinstance(result, AutoConfig):  # pragma: no cover - defensive
        raise StrategyValidationError("wrong model")
    return result  # type: ignore[return-value]
