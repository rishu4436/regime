"""StockAgentX402 with a fake baw runner and httpx.MockTransport. No network, no spend."""
from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import pytest

from backend.exchange import x402 as X
from backend.exchange.baw import BawCli, BawError
from backend.exchange.tests._baw_fakes import SETTINGS, FakeBaw, err, ok

SAMPLES = Path(__file__).parent / "samples"
REAL_402 = json.loads((SAMPLES / "x402_analyze_402.json").read_text())
REAL_PRICE = json.loads((SAMPLES / "x402_price.json").read_text())
U = "0xcE24439F2D9C6a2289F741120FE202248B666666"
USDT = "0x55d398326f99059fF775485246999027B3197955"
PAY_TO = X.PINNED_PAY_TO
TX = "0x" + "cd" * 32


def option(index=0, symbol="U", address=U, amount="0.1", status="READY_TO_SIGN", balance="5", approve=False,
           pay_to=PAY_TO):
    return {"index": index, "status": status, "reasons": [], "scheme": "exact", "assetTransferMethod": "eip3009",
            "binanceChainId": "56", "amount": amount, "tokenSymbol": symbol, "tokenAddress": address,
            "amountUsd": amount, "payTo": pay_to, "currentBalance": balance, "currentBalanceUsd": balance,
            "userWalletAddress": "0x1111111111111111111111111111111111111111", "needApproveFirst": approve}


PREVIEW = {"paymentId": "550e8400-e29b-41d4-a716-446655440000",
           "options": [option(0), option(3, "USDT", USDT, balance="40", approve=False)]}
SIGNED = {"paymentHeaderName": "PAYMENT-SIGNATURE", "paymentHeaderValue": "c2lnbmVk", "signatureExpiresAt": 1791700000}


class Svc:
    """Fake stock-agent: unpaid POST -> the real captured 402; paid POST -> 202 job; jobs poll."""

    def __init__(self, paid_status=202, job_status="running", poll_error=False):
        self.requests: list[httpx.Request] = []
        self.paid_status = paid_status if isinstance(paid_status, list) else [paid_status]
        self.job_status, self.poll_error = job_status, poll_error

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        p = req.url.path
        if p == "/x402/price":
            return httpx.Response(200, json=REAL_PRICE)
        if p == "/x402/analyze/async":
            if "payment-signature" not in req.headers:
                return httpx.Response(402, json=REAL_402["body"], headers=REAL_402["headers"])
            st = self.paid_status.pop(0) if len(self.paid_status) > 1 else self.paid_status[0]
            if st == 202:
                settle = base64.b64encode(json.dumps({"success": True, "transaction": TX,
                                                      "network": "eip155:56"}).encode()).decode()
                return httpx.Response(202, json={"jobId": "job-1", "jobToken": "tok-1"},
                                      headers={"payment-response": settle})
            return httpx.Response(st, json={"error": "settlement_pending"})
        if p == "/x402/jobs/job-1":
            if self.poll_error:
                raise httpx.ConnectError("boom")
            assert req.headers["x-job-token"] == "tok-1"
            if self.job_status == "succeeded":
                return httpx.Response(200, json={"status": "succeeded", "downloadUrl": "https://s3.example/r.md"})
            if self.job_status == "failed":
                return httpx.Response(200, json={"status": "failed", "error": "model error", "retryable": True})
            return httpx.Response(200, json={"status": self.job_status})
        if req.url.host == "s3.example":
            return httpx.Response(200, text="# NVDA\nRating: Buy")
        return httpx.Response(404)


def make(tmp_path, svc=None, baw=None, **env):
    e = {"STOCK_AGENT_X402_ENABLED": "true", **env}
    svc = svc or Svc()
    baw = baw or FakeBaw()
    if "x402-payment preview" not in baw.h:
        baw.set("x402-payment preview", ok(PREVIEW))
    if "x402-payment sign" not in baw.h:
        baw.set("x402-payment sign", ok(SIGNED))
    paid = []
    c = X.StockAgentX402(baw=BawCli(runner=baw, cmd=["baw"]), http=httpx.Client(transport=httpx.MockTransport(svc)),
                         state_dir=str(tmp_path), env=e, on_paid=paid.append, sleep=lambda s: None)
    return c, svc, baw, paid


def state(tmp_path):
    return json.loads((tmp_path / X.STATE_FILE).read_text())


def test_happy_path(tmp_path):
    c, svc, baw, paid = make(tmp_path)
    out = c.analyze("NVDAB")
    assert out == {"job_id": "job-1", "paid_usdt": 0.1, "tx_hash": TX}
    assert baw.cmds() == ["wallet status", "x402-payment preview", "wallet settings", "x402-payment sign"]
    pv = baw.argv("x402-payment preview")
    assert pv[pv.index("--paymentRequirements") + 1] == REAL_402["headers"]["payment-required"]
    sg = baw.argv("x402-payment sign")
    assert sg[sg.index("--paymentId") + 1] == PREVIEW["paymentId"] and sg[sg.index("--selectedIndex") + 1] == "0"
    bodies = [json.loads(r.content) for r in svc.requests if r.url.path == "/x402/analyze/async"]
    assert bodies == [{"symbols": ["NVDA"], "analysis_type": "comprehensive"}] * 2
    assert svc.requests[-1].headers["payment-signature"] == "c2lnbmVk"
    st = state(tmp_path)
    assert st["jobs"]["job-1"]["job_token"] == "tok-1" and "pending" not in st
    assert c.calls_today() == 1 and c.limits_dirty and paid == [out]
    assert not any("market-order" in " ".join(a) for a in baw.calls)


def test_job_poll_and_report(tmp_path):
    c, svc, _, _ = make(tmp_path)
    c.analyze("NVDA")
    assert c.job("job-1") == {"status": "pending", "report": None, "error": None}
    svc.job_status = "succeeded"
    assert c.job("job-1") == {"status": "done", "report": "# NVDA\nRating: Buy", "error": None}
    n = len(svc.requests)
    assert c.job("job-1")["status"] == "done" and len(svc.requests) == n  # cached, presigned URL not refetched
    svc.job_status = "failed"
    c2, _, _, _ = make(tmp_path, svc=svc)
    c2._set_job("job-1", status="pending")
    r = c2.job("job-1")
    assert r["status"] == "failed" and "retryable" in r["error"]
    with pytest.raises(X.X402Error) as e:
        c.job("nope")
    assert e.value.reason == X.JOB_NOT_FOUND


def test_job_persisted_even_if_poll_fails(tmp_path):
    c, svc, _, _ = make(tmp_path, svc=Svc(poll_error=True))
    c.analyze("NVDA")
    with pytest.raises(X.X402Error) as e:
        c.job("job-1")
    assert e.value.reason == X.SERVICE_UNAVAILABLE
    assert state(tmp_path)["jobs"]["job-1"]["job_token"] == "tok-1"


def test_disabled(tmp_path):
    c, svc, baw, _ = make(tmp_path, STOCK_AGENT_X402_ENABLED="false")
    with pytest.raises(X.X402Error) as e:
        c.analyze("NVDA")
    assert e.value.reason == X.X402_DISABLED and baw.calls == [] and svc.requests == []
    p = c.price("NVDAB")
    assert p == {"enabled": False, "price_usdt": 0.1, "cap_usdt": 0.1, "calls_left_today": 5, "quota_usdt_left": None}


def test_price_shape_enabled(tmp_path):
    c, _, _, _ = make(tmp_path)
    assert c.price() == {"enabled": True, "price_usdt": 0.1, "cap_usdt": 0.1, "calls_left_today": 5,
                         "quota_usdt_left": 20.0}
    assert c.quota() == {"daily_limit": 20.0, "used": 0.0, "left": 20.0, "date": "2026-10-11"}


def test_price_over_cap_refused_before_sign(tmp_path):
    baw = FakeBaw()
    baw.set("x402-payment preview", ok({"paymentId": "p", "options": [option(0, amount="0.21")]}))
    c, _, baw, _ = make(tmp_path, baw=baw)
    with pytest.raises(X.X402Error) as e:
        c.analyze("NVDA")
    assert e.value.reason == X.PRICE_OVER_CAP
    assert "x402-payment sign" not in baw.cmds() and c.calls_today() == 0


def test_402_wei_amount_also_checked(tmp_path):
    """Preview says 0.1 but a cap below the 402's own wei amount still refuses."""
    c, _, baw, _ = make(tmp_path, STOCK_AGENT_X402_MAX_PRICE="0.05")
    with pytest.raises(X.X402Error) as e:
        c.analyze("NVDA")
    assert e.value.reason == X.PRICE_OVER_CAP and "x402-payment sign" not in baw.cmds()


def test_daily_cap(tmp_path):
    c, _, baw, _ = make(tmp_path, STOCK_AGENT_X402_DAILY_CALLS="2")
    c.analyze("NVDA")
    c.analyze("TSLA")
    with pytest.raises(X.X402Error) as e:
        c.analyze("AAPL")
    assert e.value.reason == X.DAILY_CAP
    assert baw.cmds().count("x402-payment sign") == 2
    assert c.price()["calls_left_today"] == 0


def test_session_expired(tmp_path):
    baw = FakeBaw(wallet_status=err(10003001, "SESSION_EXPIRED", "session expired, please sign in"))
    c, svc, baw, _ = make(tmp_path, baw=baw)
    with pytest.raises(X.X402Error) as e:
        c.analyze("NVDA")
    assert e.value.reason == "session_expired" and svc.requests == []
    baw2 = FakeBaw()
    baw2.set("x402-payment sign", err(10003002, "NOT_SIGNED_IN", "not signed in"))
    c2, _, _, _ = make(tmp_path / "b", baw=baw2)
    with pytest.raises(X.X402Error) as e:
        c2.analyze("NVDA")
    assert e.value.reason == "session_expired" and c2.calls_today() == 0  # slot released, nothing signed


def test_guards_refuse_before_sign(tmp_path):
    c, _, baw, _ = make(tmp_path)
    for kw in ({"emergency_stop": True}, {"wallet_blocked": True}, {"guard": lambda ctx: "risk says no"}):
        with pytest.raises(X.X402Error) as e:
            c.analyze("NVDA", **kw)
        assert e.value.reason == X.GUARD_BLOCKED
    seen = []
    c.analyze("NVDA", guard=lambda ctx: seen.append(dict(ctx)) or None)
    assert seen[-1]["stage"] == "before_sign" and seen[-1]["price"] == 0.1 and seen[-1]["token"] == "U"
    assert baw.cmds().count("x402-payment sign") == 1


def test_usdt_exit_reserve(tmp_path):
    baw = FakeBaw()
    baw.set("x402-payment preview", ok({"paymentId": "p", "options": [option(3, "USDT", USDT, balance="2")]}))
    c, _, baw, _ = make(tmp_path, baw=baw, STOCK_AGENT_X402_USDT_RESERVE="5")
    with pytest.raises(X.X402Error) as e:
        c.analyze("NVDA")
    assert e.value.reason == X.EXIT_RESERVE and "x402-payment sign" not in baw.cmds()


def test_prefers_u_over_usdt_and_refuses_approve_and_foreign_payee(tmp_path):
    baw = FakeBaw()
    baw.set("x402-payment preview", ok({"paymentId": "p", "options": [option(3, "USDT", USDT), option(0, "U", U)]}))
    c, _, baw, _ = make(tmp_path, baw=baw)
    c.analyze("NVDA")
    sg = baw.argv("x402-payment sign")
    assert sg[sg.index("--selectedIndex") + 1] == "0"
    for opts, reason in (([option(2, "USDC", approve=True)], X.APPROVE_REQUIRED),
                         ([option(0, pay_to="0x" + "ee" * 20)], X.PAY_TO_MISMATCH),
                         ([option(0, status="NOT_SIGNABLE")], X.NO_SIGNABLE_OPTION)):
        b = FakeBaw()
        b.set("x402-payment preview", ok({"paymentId": "p", "options": opts}))
        c2, _, b, _ = make(tmp_path / reason, baw=b)
        with pytest.raises(X.X402Error) as e:
            c2.analyze("NVDA")
        assert e.value.reason == reason and "x402-payment sign" not in b.cmds()


def test_quota_short(tmp_path):
    baw = FakeBaw(wallet_settings=ok({**SETTINGS, "x402QuotaUsed": 19.95, "x402QuotaLeft": 0.05}))
    c, _, baw, _ = make(tmp_path, baw=baw)
    with pytest.raises(X.X402Error) as e:
        c.analyze("NVDA")
    assert e.value.reason == X.QUOTA_EXCEEDED and "x402-payment sign" not in baw.cmds()


def test_settlement_pending_replays_same_proof(tmp_path):
    c, svc, baw, _ = make(tmp_path, svc=Svc(paid_status=[503, 503, 202]))
    assert c.analyze("NVDA")["job_id"] == "job-1"
    assert baw.cmds().count("x402-payment sign") == 1
    paid = [r for r in svc.requests if "payment-signature" in r.headers]
    assert len(paid) == 3 and {r.headers["payment-signature"] for r in paid} == {"c2lnbmVk"}


def test_failed_replay_keeps_proof_and_resume_pending(tmp_path):
    svc = Svc(paid_status=[500])
    c, svc, baw, _ = make(tmp_path, svc=svc)
    with pytest.raises(X.X402Error) as e:
        c.analyze("NVDA")
    assert e.value.reason == X.PAYMENT_FAILED and state(tmp_path)["pending"]["header_value"] == "c2lnbmVk"
    assert c.calls_today() == 1  # may have been charged: counted
    svc.paid_status = [202]
    assert c.resume_pending()["job_id"] == "job-1"
    assert baw.cmds().count("x402-payment sign") == 1 and "pending" not in state(tmp_path)


def test_state_write_failure_surfaces_credentials(tmp_path, monkeypatch):
    c, _, _, _ = make(tmp_path)
    real = c._save

    def flaky(d):
        if "jobs" in d:
            raise OSError("disk full")
        real(d)
    monkeypatch.setattr(c, "_save", flaky)
    with pytest.raises(X.X402Error) as e:
        c.analyze("NVDA")
    assert e.value.reason == X.STATE_WRITE_FAILED and "tok-1" in str(e.value)


def test_baw_client_cannot_trade(tmp_path):
    c, _, baw, _ = make(tmp_path)
    with pytest.raises(BawError):
        c.baw.swap("USDT", "NVDAB", 1, 1)
    assert baw.calls == []


def test_baw_flags_and_dict_requirements():
    fb = FakeBaw()
    fb.set("x402-payment preview", ok(PREVIEW))
    fb.set("x402-payment sign", ok(SIGNED))
    cli = BawCli(runner=fb, cmd=["baw"])
    req = REAL_402["body"]["paymentRequired"]
    cli.x402_preview(req)
    v = fb.argv("x402-payment preview")[4]
    assert json.loads(base64.b64decode(v)) == req
    assert cli.x402_sign("pid", 2) == SIGNED
    assert fb.argv("x402-payment sign") == ["baw", "x402-payment", "sign", "--paymentId", "pid", "--selectedIndex", "2",
                                             "--json"]


def test_real_402_contract():
    """The captured unpaid 402 (2026-10-11): header and body carry the same PaymentRequired; our payee is pinned."""
    assert REAL_402["status"] == 402
    hdr = json.loads(base64.b64decode(REAL_402["headers"]["payment-required"]))
    body = REAL_402["body"]["paymentRequired"]
    assert hdr == body and body["x402Version"] == 2
    for a in body["accepts"]:
        assert {"scheme", "network", "amount", "asset", "payTo", "maxTimeoutSeconds", "extra"} <= set(a)
        assert a["payTo"].lower() == X.PINNED_PAY_TO and a["network"] == "eip155:56"
        assert int(a["amount"]) / 1e18 <= 0.1
    methods = {a["extra"]["name"]: a["extra"]["assetTransferMethod"] for a in body["accepts"]}
    assert methods["United Stables"] == "eip3009"
    assert REAL_PRICE["price_u"] == "0.1" and REAL_PRICE["payTo"].lower() == X.PINNED_PAY_TO


@pytest.mark.parametrize("sym,want", [("NVDAB", "NVDA"), ("NVDAon", "NVDA"), ("TSLAx", "TSLA"), ("AAPL", "AAPL"),
                                      ("SPYon", "SPY"), ("msft", "MSFT")])
def test_underlying(sym, want):
    assert X.underlying_ticker(sym) == want


def test_bad_symbol():
    with pytest.raises(X.X402Error):
        X.underlying_ticker("NVDA; rm -rf /")
