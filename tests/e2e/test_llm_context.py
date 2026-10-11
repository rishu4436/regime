"""/api/llm/summary and /api/llm/ask pass performance + alerts to explainers that accept them (Bot 5, PR #1),
and still work with explainers whose signatures don't (old Protocol shape / stand-in)."""
from __future__ import annotations

from typing import Optional

from fastapi.testclient import TestClient

from backend.app.fallbacks import FallbackExplainer
from backend.app.main import create_app
from backend.app.settings import Settings
from backend.app.wiring import ResilientExplainer, build_components
from backend.contracts import Alert, Explainer, PerformanceSummary, StatusSnapshot
from backend.mocks import MockExchangeClient

from .helpers import SEED, tick_until, trading_strategy


class NewStyle(FallbackExplainer):
    def __init__(self) -> None:
        self.seen: list[dict] = []

    def summarize_state(self, status: StatusSnapshot, *, performance: Optional[PerformanceSummary] = None,
                        alerts: Optional[list[Alert]] = None) -> str:
        self.seen.append({"performance": performance, "alerts": alerts})
        return "new-summary"

    def answer(self, question: str, status: StatusSnapshot, *, performance: Optional[PerformanceSummary] = None,
               alerts: Optional[list[Alert]] = None) -> str:
        self.seen.append({"performance": performance, "alerts": alerts, "q": question})
        return "new-answer"


class OldStyle(FallbackExplainer):
    def summarize_state(self, status: StatusSnapshot) -> str:  # no performance / alerts kwargs
        return "old-summary"

    def answer(self, question: str, status: StatusSnapshot) -> str:
        return "old-answer"


def _client(explainer) -> TestClient:
    s = Settings(sim_seed=SEED)
    comps = build_components(s, exchange=MockExchangeClient(seed=SEED, n_candles=s.sim_replay_bars), explainer=explainer)
    c = TestClient(create_app(s, comps))
    c.post("/api/manual/activate", json={"strategy": trading_strategy(activation="AUTO_EXECUTE").model_dump(mode="json")})
    assert tick_until(c, lambda d: d["action"] == "SELL")[0] is not None
    c.post("/api/control/pause", json={"reason": "check alerts"})
    return c


def test_new_style_explainer_gets_performance_and_alerts():
    for wrap in (False, True):  # injected directly, and behind the app's ResilientExplainer
        new = NewStyle()
        exp = ResilientExplainer(new, FallbackExplainer()) if wrap else new
        assert isinstance(exp, Explainer)
        c = _client(exp)
        assert c.get("/api/llm/summary").json() == {"summary": "new-summary"}
        assert c.post("/api/llm/ask", json={"question": "how am I doing?"}).json() == {"answer": "new-answer"}
        assert len(new.seen) == 2
        for kw in new.seen:
            perf = kw["performance"]
            assert isinstance(perf, PerformanceSummary) and perf.num_trades >= 1
            assert perf == PerformanceSummary.model_validate(c.get("/api/performance").json())
            assert kw["alerts"] and all(isinstance(a, Alert) for a in kw["alerts"])
            assert any(a.code == "PAUSED" for a in kw["alerts"])
        assert new.seen[1]["q"] == "how am I doing?"


def test_old_style_explainer_still_works():
    for exp in (OldStyle(), ResilientExplainer(OldStyle(), FallbackExplainer())):
        c = _client(exp)
        assert c.get("/api/llm/summary").json() == {"summary": "old-summary"}
        assert c.post("/api/llm/ask", json={"question": "?"}).json() == {"answer": "old-answer"}


def test_stand_in_uses_performance_and_alerts():
    c = _client(ResilientExplainer(None, FallbackExplainer()))
    s = c.get("/api/llm/summary").json()["summary"]
    assert "Performance:" in s and "Latest alert:" in s
