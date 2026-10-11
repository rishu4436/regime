"""Dashboard sign-in (`WalletConnect`): baw auth signin + background verify, with fakes (no real baw, no network)."""
from __future__ import annotations

import json

from backend.exchange.baw import BawCli, RunResult
from backend.exchange.wallet_connect import CONNECTED, EXPIRED, FAILED, PENDING, WalletConnect

ADDR = "0xc44edDcFfA4227d38bc92a7cA5990953AA7Ff0cF"
OTHER = "0x1111111111111111111111111111111111111111"


def ok(data):
    return RunResult(0, json.dumps({"success": True, "data": data}))


class FakeRunner:
    def __init__(self, status="CONNECTED", address=ADDR, signin=None):
        self.status, self.address = status, address
        self.signin = signin or {"urlForWeb": "https://app.binance.com/qr/abc", "qrCodeId": "q-1", "pairingCode": "1234"}
        self.calls = []

    def __call__(self, argv, timeout_s):
        args = [a for a in argv if a not in ("baw", "--json")]
        self.calls.append(args)
        if args[:2] == ["auth", "signin"]:
            return ok(self.signin)
        if args[:2] == ["wallet", "status"]:
            return ok({"status": self.status})
        if args[:2] == ["wallet", "address"]:
            return ok({"addresses": [{"binanceChainId": "56", "chainName": "BSC", "address": self.address}]})
        raise AssertionError(f"unexpected baw call {args}")


class FakeProc:
    def __init__(self):
        self.rc, self.out, self.err, self.killed = None, "", "", False

    def poll(self):
        return self.rc

    def communicate(self, timeout=None):
        return self.out, self.err

    def kill(self):
        self.killed, self.rc = True, -9


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def make(expected=ADDR, **runner_kw):
    procs, argvs = [], []

    def launcher(argv):
        argvs.append(list(argv))
        p = FakeProc()
        procs.append(p)
        return p

    clock = Clock()
    runner = FakeRunner(**runner_kw)
    wc = WalletConnect(cli=BawCli(runner=runner, cmd=["baw"]), expected_address=expected, launcher=launcher,
                       clock=clock, ttl_s=300)
    return wc, procs, argvs, clock, runner


def test_start_returns_qr_and_launches_verify():
    wc, procs, argvs, _, _ = make()
    s = wc.start()
    assert s["qr_url"] == "https://app.binance.com/qr/abc" and s["pairing_code"] == "1234"
    assert s["session_id"] and s["expires_at"].endswith("Z")
    assert argvs == [["baw", "auth", "verify", "--qrCodeId", "q-1", "--json"]]
    assert wc.poll(s["session_id"])["status"] == PENDING


def test_connected_same_wallet():
    wc, procs, _, _, _ = make()
    s = wc.start()
    procs[0].rc, procs[0].out = 0, json.dumps({"success": True, "data": {"status": "CONNECTED"}})
    r = wc.poll(s["session_id"])
    assert r == {"status": CONNECTED, "address": ADDR, "wallet_changed": False, "error": None}
    assert wc.poll(s["session_id"]) == r  # result is cached


def test_connected_case_insensitive_match():
    wc, procs, _, _, _ = make(expected=ADDR.lower())
    s = wc.start()
    procs[0].rc = 0
    assert wc.poll(s["session_id"])["wallet_changed"] is False


def test_different_wallet_flags_change():
    wc, procs, _, _, _ = make(address=OTHER)
    s = wc.start()
    procs[0].rc = 0
    r = wc.poll(s["session_id"])
    assert r["status"] == CONNECTED and r["address"] == OTHER and r["wallet_changed"] is True


def test_no_expected_address_never_flags_change():
    wc, procs, _, _, _ = make(expected="", address=OTHER)
    s = wc.start()
    procs[0].rc = 0
    assert wc.poll(s["session_id"])["wallet_changed"] is False


def test_times_out_and_kills_verify():
    wc, procs, _, clock, _ = make()
    s = wc.start()
    clock.t += 301
    r = wc.poll(s["session_id"])
    assert r["status"] == EXPIRED and procs[0].killed


def test_verify_error_is_failed_or_expired():
    wc, procs, _, _, _ = make()
    s = wc.start()
    procs[0].rc = 1
    procs[0].out = json.dumps({"success": False, "error": {"code": 1, "name": "X", "message": "QR code expired"}})
    assert wc.poll(s["session_id"])["status"] == EXPIRED
    s2 = wc.start()
    procs[1].rc, procs[1].out = 1, json.dumps({"success": False, "error": {"message": "user rejected"}})
    r = wc.poll(s2["session_id"])
    assert r["status"] == FAILED and "rejected" in r["error"]


def test_not_connected_after_verify_is_failed():
    wc, procs, _, _, _ = make(status="CREATING")
    s = wc.start()
    procs[0].rc = 0
    r = wc.poll(s["session_id"])
    assert r["status"] == FAILED and "CREATING" in r["error"]


def test_new_start_cancels_previous():
    wc, procs, _, _, _ = make()
    s1 = wc.start()
    s2 = wc.start()
    assert procs[0].killed and wc.poll(s1["session_id"])["status"] == FAILED
    assert wc.poll(s2["session_id"])["status"] == PENDING


def test_unknown_session():
    wc, *_ = make()
    assert wc.poll("nope")["status"] == FAILED


def test_signin_without_qr_raises():
    import pytest
    from backend.exchange.baw import BawError
    wc, procs, *_ = make(signin={"foo": 1})
    with pytest.raises(BawError):
        wc.start()
    assert procs == []
