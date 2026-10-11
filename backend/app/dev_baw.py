"""Fake `baw` for SIM/PAPER demos and tests of the dashboard's "Connect wallet" flow. Never touches Binance.

Implements just what backend.exchange.wallet_connect.WalletConnect calls, with baw 1.10.0's JSON envelope:
  auth signin --json                        -> {qrCodeId, urlForWeb, pairingCode}
  auth verify --qrCodeId <id> --json        -> waits FAKE_BAW_VERIFY_S (default 6 s), then FAKE_BAW_OUTCOME
                                               ok (default) | expired | failed
  wallet status --json                      -> CONNECTED
  wallet address --json                     -> FAKE_BAW_ADDRESS on BSC (chain 56)
Anything else exits 127 (a fake wallet can't trade). The app uses it automatically outside LIVE; LIVE always uses
the real baw (backend.exchange.baw.resolve_baw_command).
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid

DEFAULT_ADDRESS = "0x5151515151515151515151515151515151515151"


def _ok(data: dict) -> int:
    print(json.dumps({"success": True, "data": data}))
    return 0


def _err(msg: str, name: str = "Error", rc: int = 1) -> int:
    print(json.dumps({"success": False, "error": {"name": name, "message": msg}}))
    return rc


def main(argv: list[str]) -> int:
    args = [a for a in argv if a != "--json"]
    key = tuple(args[:2])
    if key == ("auth", "signin"):
        qid = "sim-" + uuid.uuid4().hex[:12]
        return _ok({"qrCodeId": qid, "urlForWeb": f"https://example.invalid/regime-sim-wallet-login?qrCodeId={qid}",
                    "pairingCode": "SIM-" + qid[-4:].upper()})
    if key == ("auth", "verify"):
        time.sleep(float(os.environ.get("FAKE_BAW_VERIFY_S", "6")))
        outcome = os.environ.get("FAKE_BAW_OUTCOME", "ok").lower()
        if outcome == "expired":
            return _err("QR code expired", "QrCodeExpired")
        if outcome == "failed":
            return _err("sign-in rejected in the Binance app", "SigninRejected")
        return _ok({"status": "CONNECTED"})
    if key == ("wallet", "status"):
        return _ok({"status": "CONNECTED"})
    if key == ("wallet", "address"):
        return _ok({"addresses": [{"binanceChainId": "56", "address": os.environ.get("FAKE_BAW_ADDRESS") or DEFAULT_ADDRESS}]})
    print(json.dumps({"success": False, "error": {"name": "FakeBaw", "message": f"fake baw does not support {' '.join(args)}"}}))
    return 127


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
