import json

import pytest

from backend.contracts import (
    ActivationMode, AutoConfig, BreakoutDirection, ConditionLogic, ManualStrategy, PresetName, Timeframe, TraderStyle,
)
from backend.llm.parser import (
    StrategyValidationError, detect_symbol, extract_json, keyword_parse, validate_llm_strategy,
)


def _roundtrip(m):
    type(m).model_validate_json(m.model_dump_json())  # must satisfy the contract schema


# ------------------------------------------------------------------ keyword parser
def test_manual_full_sentence():
    s = keyword_parse("Buy NVIDIA when RSI drops below 30 and price is above the 200 SMA, take profit 10%, "
                      "stop loss 5%, trailing stop 3%, sell when RSI above 75, position size 10%")
    assert isinstance(s, ManualStrategy) and s.symbol == "NVDAon"
    assert s.entry.rsi_below.enabled and s.entry.rsi_below.threshold == 30
    assert s.entry.price_above_sma.enabled and s.entry.price_above_sma.period == 200
    assert not s.entry.macd_bullish_cross.enabled and not s.entry.price_above_ema.enabled
    assert (s.exit.take_profit_pct, s.exit.stop_loss_pct) == (10, 5)
    assert s.exit.trailing_stop.enabled and s.exit.trailing_stop.pct == 3
    assert s.exit.rsi_above.threshold == 75
    assert s.risk.position_size_pct == 10
    assert s.activation == ActivationMode.CONFIRMATION
    _roundtrip(s)


def test_manual_or_logic_and_risk_and_filters():
    s = keyword_parse("MACD cross or bollinger lower band on AAPLB, any of, 4h, max 2 positions, cooldown 3 bars, "
                      "market hours, minimum volume 1000, max loss per trade 2%")
    assert s.symbol == "AAPLB" and s.timeframe == Timeframe.H4 and s.entry.logic == ConditionLogic.OR
    assert s.entry.macd_bullish_cross.enabled and s.entry.bollinger_lower.enabled
    assert not s.entry.rsi_below.enabled and not s.entry.price_above_sma.enabled
    assert s.risk.max_open_positions == 2 and s.risk.cooldown_bars == 3 and s.risk.max_loss_per_trade_pct == 2
    assert s.filters.market_hours.enabled and s.filters.min_volume.enabled and s.filters.min_volume.min_volume == 1000


def test_breakout_and_ema_and_macd_exit():
    s = keyword_parse("METAx breakout of the 30 bar high with price above EMA 21, exit on bearish MACD cross")
    assert s.symbol == "METAx" and s.entry.breakout.enabled and s.entry.breakout.lookback == 30
    assert s.entry.breakout.direction == BreakoutDirection.HIGH
    assert s.entry.price_above_ema.enabled and s.entry.price_above_ema.period == 21
    assert s.exit.macd_bearish_cross.enabled and not s.entry.macd_bullish_cross.enabled


def test_unspecified_text_gives_defaults_with_team_default_entry():
    # no entry rule in the text -> entry = price above EMA(20) only (team default); exits / risk = contract defaults
    for text in ["", "hello", "make me some money"]:
        s = keyword_parse(text, default_symbol="TSLAx")
        d = ManualStrategy(symbol="TSLAx")
        assert isinstance(s, ManualStrategy)
        enabled = [k for k, v in s.entry.model_dump().items() if isinstance(v, dict) and v.get("enabled")]
        assert enabled == ["price_above_ema"] and s.entry.price_above_ema.period == 20
        assert s.exit == d.exit and s.risk == d.risk and s.symbol == "TSLAx"


def test_auto_config():
    c = keyword_parse("auto mode, patient accumulator on tesla with $5k, only DCA and buy the dip")
    assert isinstance(c, AutoConfig) and c.symbol == "TSLAx" and c.style == TraderStyle.PATIENT
    assert c.enabled_presets == [PresetName.DCA, PresetName.BUY_THE_DIP] and c.capital == 5000
    assert c.activation == ActivationMode.CONFIRMATION


def test_auto_config_styles_and_defaults():
    c = keyword_parse("Let the agent decide, I'm an aggressive scalper, 2500 USDT on SPYon, smart DCA or grid, 15m")
    assert isinstance(c, AutoConfig) and c.style == TraderStyle.ACTIVE and c.capital == 2500 and c.symbol == "SPYon"
    assert c.enabled_presets == [PresetName.SMART_DCA, PresetName.GRID] and c.timeframe == Timeframe.M15
    c2 = keyword_parse("auto mode please")
    assert c2.style == TraderStyle.BALANCED and set(c2.enabled_presets) == set(PresetName) and c2.capital == 10000


def test_auto_execute_only_when_explicit():
    assert keyword_parse("RSI below 30 on NVDAon, auto-execute").activation == ActivationMode.AUTO_EXECUTE
    assert keyword_parse("RSI below 30 on NVDAon, ask me to confirm").activation == ActivationMode.CONFIRMATION
    assert keyword_parse("auto mode, balanced, execute without asking").activation == ActivationMode.AUTO_EXECUTE


@pytest.mark.parametrize("text,default,expected", [
    ("trade TSLAx", None, "TSLAx"), ("nvidia dip", None, "NVDAon"), ("apple", None, "AAPLB"),
    ("s&p 500 dca", None, "SPYon"), ("AMZNon breakout", None, "AMZNon"), ("something", "METAx", "METAx"), ("x", None, "NVDAon"),
])
def test_detect_symbol(text, default, expected):
    assert detect_symbol(text, default) == expected


@pytest.mark.parametrize("text", ["rsi < 999", "take profit 0%", "size 500%", "cooldown -3", "ema 1", "!!!", "x" * 10000])
def test_keyword_parser_never_raises(text):
    _roundtrip(keyword_parse(text))


# ------------------------------------------------------------------ LLM validation / repair
def test_extract_json_variants():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Sure! here: {"a": {"b": "}"}} hope that helps') == {"a": {"b": "}"}}
    assert extract_json('{"a": 1,}') == {"a": 1}
    for bad in ["", "no json here", "{not: valid"]:
        with pytest.raises(StrategyValidationError):
            extract_json(bad)


def test_validate_manual_and_safety_overrides():
    raw = json.dumps({"kind": "manual", "strategy": {
        "id": "evil", "version": 99, "symbol": "TSLAx", "activation": "AUTO_EXECUTE",
        "entry": {"logic": "or", "rsi_below": {"enabled": True, "threshold": 28}, "price_above_sma": {"enabled": False}},
        "exit": {"take_profit_pct": 8}}})
    s = validate_llm_strategy(raw, "rsi below 28 on tesla")
    assert isinstance(s, ManualStrategy) and s.symbol == "TSLAx" and s.entry.logic == ConditionLogic.OR
    assert s.entry.rsi_below.threshold == 28 and not s.entry.price_above_sma.enabled and s.exit.take_profit_pct == 8
    assert s.activation == ActivationMode.CONFIRMATION  # user didn't ask for auto-execution
    assert s.id != "evil" and s.version == 1


def test_validate_repairs_unknown_and_invalid_fields():
    raw = json.dumps({"kind": "manual", "strategy": {
        "symbol": "AAPLB", "leverage": 50, "entry": {"rsi_below": {"threshold": 250, "period": 14}, "magic": True},
        "exit": {"stop_loss_pct": -5, "take_profit_pct": 9}, "risk": {"position_size_pct": "lots"}}})
    s = validate_llm_strategy(raw, "apple")
    assert s.symbol == "AAPLB" and s.entry.rsi_below.threshold == 35  # invalid value dropped -> default
    assert s.exit.stop_loss_pct == 6 and s.exit.take_profit_pct == 9 and s.risk.position_size_pct == 15


def test_validate_auto_fills_symbol_and_capital():
    raw = '{"kind": "auto", "strategy": {"style": "patient", "enabled_presets": ["dca", "GRID"], "capital": -1}}'
    c = validate_llm_strategy(raw, "patient dca with $3000", default_symbol="SPYon")
    assert isinstance(c, AutoConfig) and c.symbol == "SPYon" and c.capital == 3000
    assert c.style == TraderStyle.PATIENT and c.enabled_presets == [PresetName.DCA, PresetName.GRID]


def test_validate_infers_kind_without_wrapper():
    c = validate_llm_strategy('{"symbol": "NVDAon", "style": "ACTIVE", "capital": 100}', "x")
    assert isinstance(c, AutoConfig) and c.style == TraderStyle.ACTIVE
    m = validate_llm_strategy('{"symbol": "NVDAon", "exit": {"take_profit_pct": 3}}', "x")
    assert isinstance(m, ManualStrategy) and m.exit.take_profit_pct == 3


def test_validate_rejects_garbage():
    for raw in ["I cannot do that", "[1,2,3]", '{"kind": "manual", "strategy": "nope"}']:
        with pytest.raises(StrategyValidationError):
            validate_llm_strategy(raw, "x")


def test_invalid_preset_list_is_dropped_to_default():
    c = validate_llm_strategy('{"kind":"auto","strategy":{"symbol":"NVDAon","capital":10,"enabled_presets":[]}}', "x")
    assert set(c.enabled_presets) == set(PresetName)
