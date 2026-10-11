"""Wallet reads on the exchange clients (beside the contract): get_balances(), get_wallet(), get_wallet_settings().

- LIVE (BawLiveClient = BinanceWeb3Client + BawExecutor): delegated to the Agentic Wallet CLI (`baw`).
- PAPER: BinanceWeb3Client has none of these methods (Bot 6 falls back to its simulated book).
- SIM: a simulated wallet derived from the client's own fills, same shapes as LIVE (provider "sim").
"""
from __future__ import annotations

import os
from datetime import timezone
from typing import Any

from backend.contracts import OrderSide, OrderStatus, utc_now

from .errors import ExchangeError

SIM_DAILY_LIMIT_USDT = 100.0


def _iso_z(dt) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class LiveWalletMixin:
    """For the LIVE client (BawLiveClient): forward wallet reads to the executor (BawExecutor)."""

    def _wallet_backend(self) -> Any:
        ex = getattr(self, "executor", None)
        if ex is None or not callable(getattr(ex, "get_wallet", None)):
            raise ExchangeError(f"no wallet reader in {getattr(self, '_mode', '?')} mode (LIVE + baw executor only)")
        return ex

    def get_balances(self) -> dict[str, float]:
        return self._wallet_backend().get_balances()

    def get_wallet(self) -> dict:
        w = dict(self._wallet_backend().get_wallet())
        if w.get("address") and not getattr(self, "wallet_address", None):
            self.wallet_address = w["address"]  # xStock/RFQ quotes need userWalletAddress
        return w

    def get_wallet_settings(self) -> dict:
        return self._wallet_backend().get_wallet_settings()


class SimWalletMixin:
    """SimulatedBinanceClient: simulated wallet = starting USDT/BNB (+ SIM_WALLET_USDT / SIM_WALLET_BNB env)
    adjusted by this client's FILLED executions."""

    sim_wallet_usdt: float = float(os.getenv("SIM_WALLET_USDT", "1000") or 1000)
    sim_wallet_bnb: float = float(os.getenv("SIM_WALLET_BNB", "0.05") or 0.05)

    def get_balances(self) -> dict[str, float]:
        bal = {"USDT": self.sim_wallet_usdt, "BNB": self.sim_wallet_bnb}
        for r in getattr(self, "executions", []):
            if r.status != OrderStatus.FILLED:
                continue
            sign = 1 if r.side == OrderSide.BUY else -1
            bal["USDT"] -= sign * r.notional  # BUY spends notional (fee included), SELL receives it
            bal[r.symbol] = bal.get(r.symbol, 0.0) + sign * r.filled_quantity
        return {k: round(v, 10) for k, v in bal.items() if abs(v) > 1e-12}

    def _sim_used_today(self) -> float:
        today = utc_now().date()
        return sum(r.notional for r in getattr(self, "executions", [])
                   if r.status == OrderStatus.FILLED and r.timestamp.date() == today)

    def get_wallet_settings(self) -> dict:
        used = round(self._sim_used_today(), 8)
        now = utc_now()
        return {"dailyLimit": SIM_DAILY_LIMIT_USDT, "quotaUsed": used, "quotaLeft": round(SIM_DAILY_LIMIT_USDT - used, 8),
                "quotaDate": now.date().isoformat(), "tradeAllTokens": True, "abnormalTxnHandling": "AutoReject",
                "maxSigninDuration": None, "sessionExpireTime": None, "fetched_at": _iso_z(now)}

    def get_wallet(self) -> dict:
        bal = self.get_balances()
        rows = []
        for sym, amt in sorted(bal.items(), key=lambda kv: (kv[0] not in ("BNB", "USDT"), kv[0])):
            if sym == "USDT":
                usd = amt
            elif sym == "BNB":
                usd = None
            else:
                try:
                    usd = amt * self.get_price(sym)  # type: ignore[attr-defined]
                except Exception:  # noqa: BLE001
                    usd = None
            rows.append({"symbol": sym, "amount": amt, "usd_value": None if usd is None else round(usd, 8)})
        s = self.get_wallet_settings()
        return {"provider": "sim", "address": "0xSIMULATED-WALLET", "chain": "BSC", "chain_id": 56, "connected": True,
                "status": "CONNECTED", "balances": rows, "bnb_gas_reserve": 0.002,
                "limits": {"daily_limit_usdt": s["dailyLimit"], "daily_used_usdt": s["quotaUsed"],
                           "daily_left_usdt": s["quotaLeft"], "per_order_cap_usdt": None, "allowed_tokens": "ALL",
                           "trade_all_tokens": True, "quota_date": s["quotaDate"],
                           "limit_source": {"daily_limit_usdt": "wallet", "per_order_cap_usdt": "agent"}},
                "session_expires_at": None, "abnormal_txn_handling": "AutoReject", "warnings": [], "error": None,
                "updated_at": s["fetched_at"]}
