import ast
import json
from pathlib import Path

import httpx
import pytest

from backend.contracts import ActivationMode, AutoConfig, Explainer, ManualStrategy, TraderStyle
from backend.llm import DefaultExplainer, templates
from backend.llm.client import GrokClient, GrokConfig

LLM_DIR = Path(__file__).resolve().parents[1]


def test_conforms_to_protocol():
    assert isinstance(DefaultExplainer(), Explainer)


def test_no_key_uses_templates_and_no_network(buy_decision, status, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network used without a key")
    monkeypatch.setattr(httpx.Client, "post", boom)
    ex = DefaultExplainer()
    assert not ex.use_llm
    assert ex.explain_decision(buy_decision) == templates.explain_decision(buy_decision)
    assert ex.summarize_state(status) == templates.summarize_state(status)
    assert ex.answer("what regime?", status) == templates.answer("what regime?", status)
    assert isinstance(ex.parse_strategy_from_text("RSI below 25"), ManualStrategy)
    assert ex.last_source == "template"


def test_env_key_enables_llm(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "k")
    assert DefaultExplainer().use_llm
    assert not DefaultExplainer(use_llm=False).use_llm


def test_grok_success_paths(make_client, reply, buy_decision, status):
    client, rec = make_client(reply("Grok says: bought NVDAon because the trend is strong."))
    ex = DefaultExplainer(client)
    assert ex.explain_decision(buy_decision).startswith("Grok says") and ex.last_source == "grok"
    assert ex.summarize_state(status).startswith("Grok says")
    assert ex.answer("how am I doing?", status).startswith("Grok says")
    msgs = [p["messages"] for p in rec.payloads]
    assert all(m[0]["role"] == "system" and "NEVER execute" in m[0]["content"] for m in msgs)
    assert '"symbol":"NVDAon"' in msgs[0][1]["content"]  # facts are passed as JSON
    assert "how am I doing?" in msgs[2][1]["content"]


def test_long_llm_output_is_capped(make_client, reply, buy_decision):
    client, _ = make_client(reply("a" * 10000))
    assert len(DefaultExplainer(client).explain_decision(buy_decision)) <= 2000


@pytest.mark.parametrize("handler", [
    lambda r: httpx.Response(500),
    lambda r: httpx.Response(200, json={"weird": True}),
    lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("t", request=r)),
    lambda r: (_ for _ in ()).throw(httpx.ConnectError("c", request=r)),
])
def test_every_method_falls_back_on_failure(make_client, handler, buy_decision, status):
    client, rec = make_client(handler)
    ex = DefaultExplainer(client)
    assert ex.explain_decision(buy_decision) == templates.explain_decision(buy_decision)
    assert ex.last_source == "template" and ex.last_error
    assert ex.summarize_state(status) == templates.summarize_state(status)
    assert ex.answer("positions?", status) == templates.answer("positions?", status)
    s = ex.parse_strategy_from_text("RSI below 25 on TSLAx")
    assert isinstance(s, ManualStrategy) and s.symbol == "TSLAx" and s.entry.rsi_below.threshold == 25
    assert len(rec.requests) == 4


def test_unexpected_exception_falls_back(buy_decision):
    class Exploding(GrokClient):
        def chat(self, *a, **k):
            raise ZeroDivisionError("bug")
    ex = DefaultExplainer(Exploding(GrokConfig(api_key="k")))
    assert ex.explain_decision(buy_decision) == templates.explain_decision(buy_decision)
    assert "ZeroDivisionError" in ex.last_error


def test_parse_via_grok_validated(make_client, reply):
    body = {"kind": "auto", "strategy": {"symbol": "TSLAx", "style": "PATIENT", "enabled_presets": ["DCA"], "capital": 2000,
                                         "activation": "AUTO_EXECUTE"}}
    client, rec = make_client(reply("```json\n" + json.dumps(body) + "\n```"))
    ex = DefaultExplainer(client)
    c = ex.parse_strategy_from_text("patiently accumulate tesla with 2000 dollars using DCA")
    assert isinstance(c, AutoConfig) and c.style == TraderStyle.PATIENT and c.capital == 2000
    assert c.activation == ActivationMode.CONFIRMATION and ex.last_source == "grok"
    p = rec.payloads[0]
    assert p["temperature"] == 0.0 and "ManualStrategy JSON Schema" in p["messages"][1]["content"]


def test_parse_invalid_grok_output_falls_back_to_keywords(make_client, reply):
    client, _ = make_client(reply("Sorry, I can't help with that."))
    ex = DefaultExplainer(client)
    s = ex.parse_strategy_from_text("RSI below 22 on AAPLB, take profit 7%")
    assert isinstance(s, ManualStrategy) and s.symbol == "AAPLB" and s.entry.rsi_below.threshold == 22 and s.exit.take_profit_pct == 7
    assert ex.last_source == "template"


def test_module_never_touches_the_exchange_or_trading():
    """Static guard: backend/llm must not import exchange/risk/app code or mocks, nor call execution methods."""
    banned_imports = ("backend.exchange", "backend.risk", "backend.app", "backend.mocks", "backend.engine")
    banned_calls = {"execute_swap", "execute_rfq", "get_quote"}
    for py in LLM_DIR.glob("*.py"):
        tree = ast.parse(py.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith(banned_imports), f"{py.name} imports {node.module}"
            if isinstance(node, ast.Import):
                assert not any(a.name.startswith(banned_imports) for a in node.names), py.name
            if isinstance(node, ast.Attribute):
                assert node.attr not in banned_calls, f"{py.name} references {node.attr}"
