"""Exactly one prefix in reasons (alerts, decisions, control) and no stale confirmations across agent switches."""
from __future__ import annotations

import re

from backend.contracts import AutoConfig, ExitReason, TraderStyle
from backend.monitoring import one_prefix

from .helpers import make_client, tick_until, trading_strategy

DOUBLED = re.compile(r"(?i)\b(paused|emergency[ _]stop|blocked|rejected by user|not executed)\b\W+\1\b")
CODES = [r.value for r in ExitReason]


def _assert_single_prefixes(texts):
    for t in texts:
        assert not DOUBLED.search(t), t
        for code in CODES:
            assert t.count(code + ":") <= 1, t
        if "Emergency stop:" in t:
            assert "EMERGENCY STOP" not in t, t


def test_one_prefix_helper():
    assert one_prefix("Paused", "Paused: lunch") == "Paused: lunch"
    assert one_prefix("Paused", "Paused by user") == "Paused by user"
    assert one_prefix("Emergency stop", "EMERGENCY STOP: x") == "Emergency stop: x"
    assert one_prefix("RSI_EXIT", "RSI_EXIT: RSI(14)=83.8 > 70", keep_sentence=False) == "RSI_EXIT: RSI(14)=83.8 > 70"
    assert one_prefix("RSI_EXIT", "RSI(14)=83.8 > 70", keep_sentence=False) == "RSI_EXIT: RSI(14)=83.8 > 70"
    assert one_prefix("Blocked", "Blocked (paused): Agent is paused") == "Blocked (paused): Agent is paused"


def test_no_doubled_prefixes_in_alerts_decisions_and_control():
    c = make_client()
    c.post("/api/manual/activate", json={"strategy": trading_strategy(activation="AUTO_EXECUTE").model_dump(mode="json")})
    sell, _ = tick_until(c, lambda d: d["action"] == "SELL" and d["signal"]["exit_reason"] == "RSI_EXIT")
    assert sell is not None
    assert re.fullmatch(r"RSI_EXIT: RSI\(14\)=\d+(\.\d+)? > 60", sell["reason"]), sell["reason"]
    tick_until(c, lambda d: d["action"] == "BUY")
    texts = []
    # pause with and without a reason, blocked entries, emergency stop with and without a reason
    texts.append(c.post("/api/control/pause", json={"reason": "lunch"}).json()["reason"] or "")
    for _ in range(30):
        texts += [d["reason"] for d in c.post("/api/loop/tick").json()]
    c.post("/api/control/resume")
    texts.append(c.post("/api/control/pause").json()["reason"] or "")
    c.post("/api/control/resume")
    texts.append(c.post("/api/control/emergency-stop", json={"reason": "test"}).json()["reason"] or "")
    for _ in range(3):
        texts += [d["reason"] for d in c.post("/api/loop/tick").json()]
    c.post("/api/control/reset-emergency-stop")
    c.post("/api/control/emergency-stop")
    texts += [d["reason"] for d in c.post("/api/loop/tick").json()]
    alerts = c.get("/api/alerts?limit=500").json()
    texts += [a["message"] for a in alerts]
    texts += [d["reason"] for d in c.get("/api/decisions?limit=2000").json()]
    _assert_single_prefixes(texts)
    msgs = {a["code"]: [x["message"] for x in alerts if x["code"] == a["code"]] for a in alerts}
    assert "Paused: lunch" in msgs["PAUSED"] and "Paused by user" in msgs["PAUSED"]
    assert "Emergency stop: test" in msgs["EMERGENCY_STOP"] and "Emergency stop by user" in msgs["EMERGENCY_STOP"]
    sells = [d for d in c.get("/api/decisions?limit=2000").json() if d["action"] == "SELL"]
    assert sells
    for d in sells:
        code = d["signal"]["exit_reason"]
        assert d["reason"].startswith(code + ": ") and d["reason"].count(code) == 1, d["reason"]


def _pending(c):
    return tick_until(c, lambda d: d["execution"] and d["execution"]["status"] == "PENDING_CONFIRMATION")[0]


def test_activating_another_agent_rejects_pending_confirmations():
    c = make_client()
    c.post("/api/manual/activate", json={"strategy": trading_strategy().model_dump(mode="json")})
    p = _pending(c)
    assert p is not None
    cfg = AutoConfig(symbol="AAPLB", style=TraderStyle.PATIENT, capital=3000).model_dump(mode="json")
    assert c.post("/api/auto/activate", json=cfg).status_code == 200
    assert c.get("/api/confirmations").json() == []
    old = c.get(f"/api/decisions/{p['id']}").json()
    assert old["action"] == "STAY_OUT" and old["execution"]["status"] == "REJECTED"
    assert old["reason"] == "Not executed: agent switched"
    assert c.post(f"/api/confirmations/{p['id']}/approve").status_code == 404
    assert c.get("/api/portfolio").json()["positions"] == []

    # manual -> manual (re-activation) also clears, and so does deactivate
    p2 = _pending(c)
    assert p2 is not None
    c.post("/api/manual/activate", json={"strategy": trading_strategy().model_dump(mode="json")})
    assert c.get("/api/confirmations").json() == []
    p3 = _pending(c)
    assert p3 is not None
    c.post("/api/agent/deactivate")
    assert c.get("/api/confirmations").json() == []
    assert c.get(f"/api/decisions/{p3['id']}").json()["reason"] == "Not executed: agent deactivated"
