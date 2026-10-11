"""READ-ONLY Agentic Wallet smoke check for the trading PC. It never swaps, sends or signs anything.

    python -m backend.exchange.baw_smoke            # human summary
    python -m backend.exchange.baw_smoke --json     # machine-readable

Runs: wallet status, wallet address, wallet balance --binanceChainId 56, wallet settings,
wallet left-quota, wallet tx-lock --binanceChainId 56, market-order quote 1 USDT -> NVDAB (quote only).
The baw client is built with read_only=True, which refuses `market-order swap` and every other write command
before any process is spawned. Sign in first yourself: `baw auth signin` and scan the QR in the Binance App.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Any, Optional

from .baw import BSC_CHAIN_ID, BawCli, BawError
from .baw_executor import max_slippage_pct, wallet_snapshot
from .tokens import QUOTE_ASSETS

NVDAB_BSC = "0x02fca66c1d1afb4e2a7884261eb00f63598a7436"  # RWA list type 3 (bStock), chainId 56, 18 decimals


def run_checks(cli: Optional[BawCli] = None, quote_usdt: float = 1.0, to_token: str = NVDAB_BSC) -> dict[str, Any]:
    cli = cli or BawCli(read_only=True)
    if not cli.read_only:
        raise ValueError("baw_smoke requires a read_only BawCli")
    usdt = QUOTE_ASSETS[BSC_CHAIN_ID].address
    out: dict[str, Any] = {"cmd": None, "checks": {}, "ok": True}
    try:
        out["cmd"] = cli.cmd
    except Exception as e:  # noqa: BLE001
        out["cmd"] = f"unresolved: {e}"

    def step(name: str, fn) -> Any:
        try:
            val = fn()
            out["checks"][name] = {"ok": True, "result": val}
            return val
        except BawError as e:
            out["checks"][name] = {"ok": False, "reason": e.reason, "error": e.args[0]}
        except subprocess.TimeoutExpired:
            out["checks"][name] = {"ok": False, "reason": "baw_timeout", "error": "timed out"}
        out["ok"] = False
        return None

    status = step("status", cli.status)
    if status not in (None, "CONNECTED"):
        out["ok"] = False
        out["checks"]["status"]["ok"] = False
        out["checks"]["status"]["reason"] = "session_expired" if status == "UNCONNECTED" else "wallet_not_ready"
        out["checks"]["status"]["error"] = f"wallet status {status}: run `baw auth signin` and scan the QR in the Binance App"
    step("address", lambda: cli.address(BSC_CHAIN_ID))
    step("balance", lambda: cli.balances(BSC_CHAIN_ID))
    step("settings", cli.settings)
    step("left_quota", cli.left_quota)
    step("tx_lock", lambda: cli.tx_lock(BSC_CHAIN_ID))
    step("quote_1usdt_nvdab", lambda: cli.quote(usdt, to_token, quote_usdt, max_slippage_pct(), BSC_CHAIN_ID))
    out["wallet"] = wallet_snapshot(cli, BSC_CHAIN_ID)
    return out


def main(argv: Optional[list[str]] = None, cli: Optional[BawCli] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true", help="print the full result as JSON")
    ap.add_argument("--usdt", type=float, default=1.0, help="quote size in USDT (quote only, never traded)")
    a = ap.parse_args(argv)
    res = run_checks(cli, quote_usdt=a.usdt)
    if a.json:
        print(json.dumps(res, indent=2, default=str))
    else:
        print(f"baw command: {res['cmd']}")
        for name, c in res["checks"].items():
            mark = "OK  " if c["ok"] else "FAIL"
            detail = c.get("result") if c["ok"] else f"[{c.get('reason')}] {c.get('error', '')}"
            text = json.dumps(detail, default=str)
            print(f"  {mark} {name:<18} {text[:160]}")
        w = res["wallet"]
        print(f"wallet connected={w['connected']} address={w['address']} limits={w['limits']}")
        for warn in w["warnings"]:
            print(f"  WARNING [{warn['code']}] {warn['message']}")
        print("ALL READ-ONLY CHECKS PASSED" if res["ok"] else "SOME CHECKS FAILED (nothing was traded)")
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
