"""Minimal app so the skeleton runs. Bot 6 owns and extends this (REST + WebSocket, wiring all modules).

Run: uvicorn backend.app.main:app --reload --port 8000
"""
from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.contracts import CONTRACTS_VERSION, ControlState, TokenInfo, TradingMode
from backend.exchange.factory import get_exchange_client

app = FastAPI(title="Autonomous On-Chain Stock Trading Agent", version=CONTRACTS_VERSION)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "contracts_version": CONTRACTS_VERSION, "trading_mode": os.getenv("TRADING_MODE", "SIM")}


@app.get("/api/control", response_model=ControlState)
def control() -> ControlState:
    return ControlState(trading_mode=TradingMode(os.getenv("TRADING_MODE", "SIM")))


@app.get("/api/tokens", response_model=list[TokenInfo])
def tokens(q: str = "") -> list[TokenInfo]:
    return get_exchange_client().search_tokens(q)
