import httpx
import pytest

from backend.llm.client import DEFAULT_BASE_URL, DEFAULT_MODEL, GrokClient, GrokConfig, LLMError

from .conftest import chat_response


def test_config_from_env_defaults():
    cfg = GrokConfig.from_env()
    assert cfg.api_key is None and cfg.model == DEFAULT_MODEL and cfg.base_url == DEFAULT_BASE_URL
    assert not GrokClient(cfg).enabled


def test_config_from_env_overrides(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", " sk-secret ")
    monkeypatch.setenv("XAI_MODEL", "grok-4.3")
    monkeypatch.setenv("XAI_BASE_URL", "https://proxy.example/v1/")
    monkeypatch.setenv("XAI_TIMEOUT_S", "3.5")
    cfg = GrokConfig.from_env()
    assert (cfg.api_key, cfg.model, cfg.base_url, cfg.timeout_s) == ("sk-secret", "grok-4.3", "https://proxy.example/v1", 3.5)
    assert "sk-secret" not in repr(cfg)


def test_bad_timeout_env_and_disable_flag(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "k")
    monkeypatch.setenv("XAI_TIMEOUT_S", "abc")
    monkeypatch.setenv("LLM_DISABLED", "1")
    cfg = GrokConfig.from_env()
    assert cfg.timeout_s == 20.0 and cfg.api_key is None


def test_chat_sends_openai_compatible_request(make_client, reply):
    client, rec = make_client(reply("  hello  "))
    assert client.chat([{"role": "user", "content": "hi"}]) == "hello"
    req = rec.requests[0]
    assert str(req.url) == "https://api.x.ai/v1/chat/completions" and req.method == "POST"
    assert req.headers["authorization"] == "Bearer test-key"
    body = rec.payloads[0]
    assert body["model"] == "grok-test" and body["messages"][0]["content"] == "hi" and body["stream"] is False


def test_chat_without_key_raises_and_makes_no_request(make_client, reply):
    client, rec = make_client(reply("x"), key=None)
    with pytest.raises(LLMError):
        client.chat([{"role": "user", "content": "hi"}])
    assert rec.requests == []


@pytest.mark.parametrize("handler", [
    lambda r: httpx.Response(500, json={"error": "boom"}),
    lambda r: httpx.Response(401, json={"error": "bad key"}),
    lambda r: httpx.Response(429, json={"error": "rate"}),
    lambda r: httpx.Response(200, text="not json"),
    lambda r: httpx.Response(200, json={"choices": []}),
    lambda r: httpx.Response(200, json=chat_response("   ")),
    lambda r: httpx.Response(200, json={"choices": [{"message": {"content": None}}]}),
])
def test_chat_errors_become_llmerror(make_client, handler):
    client, _ = make_client(handler)
    with pytest.raises(LLMError):
        client.chat([{"role": "user", "content": "hi"}])


def test_timeout_and_transport_errors(make_client):
    def timeout(req):
        raise httpx.ReadTimeout("slow", request=req)

    def conn(req):
        raise httpx.ConnectError("down", request=req)

    for h in (timeout, conn):
        client, _ = make_client(h)
        with pytest.raises(LLMError):
            client.chat([{"role": "user", "content": "hi"}])


def test_error_messages_do_not_leak_key(make_client):
    client, _ = make_client(lambda r: httpx.Response(403, text="forbidden test-key"))
    with pytest.raises(LLMError) as ei:
        client.chat([{"role": "user", "content": "hi"}])
    assert "test-key" not in str(ei.value)
