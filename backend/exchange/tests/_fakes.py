from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import parse_qsl, urlparse

import httpx
import pytest

from backend.exchange.web3_api import Web3ApiClient

T0 = datetime(2026, 10, 12, 15, 0, tzinfo=timezone.utc)  # Monday, US session open


class Clock:
    def __init__(self, t: datetime = T0) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> Clock:
    return Clock()


def ok(data: Any) -> httpx.Response:
    return httpx.Response(200, json={"code": 0, "msg": "success", "data": data, "timestamp": 1, "success": True})


def err(code: int, msg: str, status: int = 400) -> httpx.Response:
    return httpx.Response(status, json={"code": code, "msg": msg, "data": None, "timestamp": 1})


class FakeBinance:
    """Routes mocked HTTP calls by path; records every request."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], Callable[[httpx.Request], httpx.Response]] = {}
        self.requests: list[httpx.Request] = []

    def on(self, method: str, path: str, handler) -> None:
        self.routes[(method, path)] = handler if callable(handler) else (lambda _r, h=handler: h)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = urlparse(str(request.url)).path.removeprefix("/build")
        for (m, p), h in self.routes.items():
            if m == request.method and (p == path or (p.endswith("*") and path.startswith(p[:-1]))):
                return h(request)
        return httpx.Response(404, json={"code": 40400, "msg": f"no fake for {request.method} {path}"})

    @staticmethod
    def params(request: httpx.Request) -> dict[str, str]:
        return dict(parse_qsl(urlparse(str(request.url)).query))

    @staticmethod
    def body(request: httpx.Request) -> Any:
        return json.loads(request.content or b"null")


@pytest.fixture
def fake() -> FakeBinance:
    return FakeBinance()


@pytest.fixture
def api(fake: FakeBinance) -> Web3ApiClient:
    return Web3ApiClient("test-key-1234", "test-secret", http=httpx.Client(transport=httpx.MockTransport(fake)),
                         sleep=lambda s: None, timestamp_fn=lambda: "2026-10-12T15:00:00.000Z")
