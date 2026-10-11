import json
import subprocess

import pytest

from backend.exchange import baw as B
from backend.exchange.baw import BawCli, BawError, BawSessionExpired, RunResult, fmt_qty, resolve_baw_command

from ._baw_fakes import ADDR, NVDAB, USDT, FakeBaw, err

# Verbatim output of the real baw 1.10.0 (node dist/index.js, not signed in) - captured 2026-10-11 on the box.
REAL_STATUS = '{\n  "success": true,\n  "data": {\n    "status": "UNCONNECTED"\n  }\n}\n'
REAL_NOT_LOGGED_IN = ('{\n  "success": false,\n  "error": {\n    "code": 10003000,\n    "name": "NOT_LOGGED_IN",\n'
                      '    "message": "Not logged in"\n  }\n}\n')


def cli(fake):
    return BawCli(runner=fake, cmd=["baw"])


def test_real_recorded_outputs_parse():
    fake = FakeBaw(wallet_status=RunResult(0, REAL_STATUS), wallet_address=RunResult(1, REAL_NOT_LOGGED_IN))
    c = cli(fake)
    assert c.status() == "UNCONNECTED"
    with pytest.raises(BawSessionExpired) as ei:
        c.address()
    assert ei.value.reason == B.SESSION_EXPIRED and ei.value.code == 10003000
    assert str(ei.value).startswith("[session_expired]")


def test_reads_and_argv_and_timeouts():
    fake = FakeBaw()
    c = cli(fake)
    assert c.status() == "CONNECTED"
    assert c.address("56") == ADDR
    assert [b["symbol"] for b in c.balances()] == ["BNB", "USDT", "NVDAB"]
    assert c.settings()["dailyLimit"] == 50
    assert c.left_quota()["quotaLeft"] == 37.7
    assert c.tx_lock() == "UNLOCKED"
    assert fake.argv("wallet balance") == ["baw", "wallet", "balance", "--binanceChainId", "56", "--json"]
    assert fake.argv("wallet tx-lock") == ["baw", "wallet", "tx-lock", "--binanceChainId", "56", "--json"]
    assert set(fake.timeouts) == {20.0}
    c.swap(USDT, NVDAB, 10, 1)
    assert fake.timeouts[-1] == 45.0


def test_quote_always_passes_explicit_slippage():
    fake = FakeBaw()
    q = cli(fake).quote(USDT, NVDAB, 1, 1)
    assert q["toCoinAmount"] == "0.0054"
    argv = fake.argv("market-order quote")
    assert argv[argv.index("--slippage") + 1] == "1" and "auto" not in argv
    assert argv[argv.index("--fromTokenQty") + 1] == "1"


def test_error_json_on_stdout_exit_1_maps_reason():
    fake = FakeBaw(market_order_swap=err(30003001, "ORDER_API_ERROR", "Exceeds daily trading limit"))
    with pytest.raises(BawError) as ei:
        cli(fake).swap(USDT, NVDAB, 10, 1)
    assert ei.value.reason == B.QUOTA_EXCEEDED


def test_non_json_output_is_structured_error():
    fake = FakeBaw(wallet_status=RunResult(1, "", "node: bad option"))
    with pytest.raises(BawError) as ei:
        cli(fake).status()
    assert ei.value.reason == B.BAW_ERROR and "non-JSON" in str(ei.value)


def test_missing_binary_is_baw_not_found():
    def runner(argv, t):
        raise FileNotFoundError("baw")
    with pytest.raises(BawError) as ei:
        BawCli(runner=runner, cmd=["baw"]).status()
    assert ei.value.reason == B.BAW_NOT_FOUND


def test_timeout_propagates_to_caller():
    fake = FakeBaw(wallet_status=subprocess.TimeoutExpired(["baw"], 20))
    with pytest.raises(subprocess.TimeoutExpired):
        cli(fake).status()


def test_read_only_refuses_swap_without_spawning():
    fake = FakeBaw()
    c = BawCli(runner=fake, cmd=["baw"], read_only=True)
    with pytest.raises(BawError):
        c.swap(USDT, NVDAB, 1, 1)
    with pytest.raises(BawError):
        c.call("wallet", "send", "--amount", "1")
    assert fake.calls == []
    c.quote(USDT, NVDAB, 1, 1)  # quotes are reads
    assert fake.cmds() == ["market-order quote"]


@pytest.mark.parametrize("code,msg,reason", [
    (10003002, "Session expired", B.SESSION_EXPIRED),
    (100001005, "x", B.SESSION_EXPIRED),
    (30006002, "Transaction in progress", B.TX_LOCKED),
    (30001004, "Confirmation required on App", B.APP_CONFIRMATION_REQUIRED),
    (30003002, "Slippage exceeded", B.SLIPPAGE_EXCEEDED),
    (30003001, "Swap reverted: INSUFFICIENT_OUTPUT_AMOUNT", B.SLIPPAGE_EXCEEDED),
    (30003001, "min receive not met", B.SLIPPAGE_EXCEEDED),
    (30003001, "Daily limit exceeded", B.QUOTA_EXCEEDED),
    (30003001, "Abnormal transaction rejected by risk control", B.ABNORMAL_TXN_REJECTED),
    (30003001, "Token is not allowed for this wallet", B.POLICY_REJECTED),
    (30001003, "Compliance check failed", B.POLICY_REJECTED),
    (30003001, "Something else", B.ORDER_REJECTED),
    (40367, "x", B.MARKET_CLOSED),
    (40374, "x", B.NO_ROUTE),
    (20002003, "Insufficient gas", B.INSUFFICIENT_GAS),
    (50001003, "Service unavailable", B.SERVICE_UNAVAILABLE),
])
def test_classify(code, msg, reason):
    assert B.classify(code, msg) == reason


def test_fmt_qty_plain_decimal_round_down():
    assert fmt_qty(1e-7) == "0.0000001"
    assert fmt_qty(25) == "25"
    assert fmt_qty("0.12345678901234567891") == "0.123456789012345678"
    assert B.fmt_slippage(0.5) == "0.5"
    with pytest.raises(ValueError):
        B.fmt_slippage(101)


def test_resolve_cmd_env_override(tmp_path):
    assert resolve_baw_command({"BAW_CMD": json.dumps(["node", "C:\\x\\index.js"])}, "win32") == ["node", "C:\\x\\index.js"]
    assert resolve_baw_command({"BAW_CMD": "/opt/baw/dist/index.js", "NODE_CMD": "node"}, "linux") == ["node", "/opt/baw/dist/index.js"]
    assert resolve_baw_command({"BAW_CMD": "/usr/local/bin/baw"}, "linux") == ["/usr/local/bin/baw"]


def test_resolve_cmd_windows_npm_global(tmp_path, monkeypatch):
    entry = tmp_path / "npm" / "node_modules" / "@binance" / "agentic-wallet" / "dist" / "index.js"
    entry.parent.mkdir(parents=True)
    entry.write_text("// baw")
    monkeypatch.setattr(B.shutil, "which", lambda name: None)
    assert resolve_baw_command({"APPDATA": str(tmp_path), "NODE_CMD": "node"}, "win32") == ["node", str(entry)]
