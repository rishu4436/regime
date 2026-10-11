from datetime import datetime

import pytest

from backend.contracts import OrderRequest, OrderSide, OrderStatus, TradingMode
from backend.exchange import SimulatedBinanceClient
from backend.exchange import baw as B
from backend.exchange import baw_smoke
from backend.exchange.baw import BawCli
from backend.exchange.baw_executor import BawExecutor, BawLiveClient, IntentStore

from ._baw_fakes import ADDR, SETTINGS, FakeBaw, err, ok

GATE_ON = {"LIVE_TRADING_ENABLED": "true", "BINANCE_LIVE_ENABLED": "true", "RISK_ALLOW_LIVE": "true"}


def ex_for(fake, tmp_path):
    return BawExecutor(cli=BawCli(runner=fake, cmd=["baw"]), intents=IntentStore(tmp_path / "i.json"), env=GATE_ON)


def test_get_wallet_bot1_shape(tmp_path):
    w = ex_for(FakeBaw(), tmp_path).get_wallet()
    assert w["provider"] == "baw" and w["address"] == ADDR and w["chain"] == "BSC" and w["chain_id"] == 56
    assert w["connected"] is True and w["bnb_gas_reserve"] == 0.002
    assert {"symbol": "USDT", "amount": 42.5, "usd_value": 42.5}.items() <= w["balances"][1].items()
    lim = w["limits"]
    assert lim["daily_limit_usdt"] == 50 and lim["daily_used_usdt"] == 12.3 and lim["daily_left_usdt"] == 37.7
    assert lim["per_order_cap_usdt"] == 25 and lim["allowed_tokens"] == "LIMITED"
    assert w["updated_at"].endswith("Z") and w["abnormal_txn_handling"] == "AutoReject"
    assert any(x["code"] == B.POLICY_REJECTED for x in w["warnings"])  # Limited scope warning


def test_get_wallet_session_expired_never_raises(tmp_path):
    for fake in (FakeBaw(wallet_status=ok({"status": "UNCONNECTED"})),
                 FakeBaw(wallet_address=err(10003002, "SESSION_EXPIRED", "Session expired"))):
        w = ex_for(fake, tmp_path).get_wallet()
        assert w["connected"] is False and w["error"]["reason"] == B.SESSION_EXPIRED


def test_get_balances_dict(tmp_path):
    assert ex_for(FakeBaw(), tmp_path).get_balances() == {"BNB": 0.012, "USDT": 42.5, "NVDAB": 0.1}


def test_get_wallet_settings_raw_plus_fetched_at(tmp_path):
    s = ex_for(FakeBaw(), tmp_path).get_wallet_settings()
    assert {k: s[k] for k in SETTINGS} == SETTINGS
    t = datetime.fromisoformat(s["fetched_at"].replace("Z", "+00:00"))
    assert t.tzinfo is not None and t.utcoffset().total_seconds() == 0


def test_wallet_settings_round_trip_bot3_from_baw(tmp_path):
    risk = pytest.importorskip("backend.risk")
    WalletLimits = getattr(risk, "WalletLimits", None)
    if WalletLimits is None or not hasattr(WalletLimits, "from_baw"):
        pytest.skip("Bot 3's WalletLimits.from_baw not on this branch")
    s = ex_for(FakeBaw(), tmp_path).get_wallet_settings()
    wl = WalletLimits.from_baw(s)
    assert wl.daily_limit_usdt == 50 and wl.daily_used_usdt == 12.3 and wl.daily_left_usdt == 37.7
    assert wl.trade_all_tokens is False and wl.quota_date == "2026-10-11"
    assert wl.fetched_at is not None and wl.fetched_at.tzinfo is not None
    assert wl.fetched_at == datetime.fromisoformat(s["fetched_at"].replace("Z", "+00:00"))
    wl2 = WalletLimits.from_baw({"success": True, "data": s})
    assert wl2.daily_limit_usdt == 50


def test_sim_wallet_methods_follow_fills():
    sim = SimulatedBinanceClient()
    w0 = sim.get_wallet()
    assert w0["provider"] == "sim" and w0["connected"] and sim.get_balances()["USDT"] == 1000
    q = sim.get_quote("AAPLB", OrderSide.BUY, 50)
    order = OrderRequest(symbol="AAPLB", side=OrderSide.BUY, notional=50, path=q.path, quote_id=q.quote_id)
    r = (sim.execute_rfq if q.path.value == "RFQ" else sim.execute_swap)(order, q)
    assert r.status == OrderStatus.FILLED
    bal = sim.get_balances()
    assert bal["USDT"] == pytest.approx(1000 - r.notional) and bal["AAPLB"] == pytest.approx(r.filled_quantity)
    s = sim.get_wallet_settings()
    assert s["quotaUsed"] == pytest.approx(r.notional) and s["fetched_at"].endswith("Z")
    assert set(sim.get_wallet()) >= {"provider", "address", "chain", "chain_id", "connected", "balances",
                                     "bnb_gas_reserve", "limits", "updated_at"}


def test_baw_live_client_delegates(tmp_path, clock, api):
    ex = ex_for(FakeBaw(), tmp_path)
    c = BawLiveClient(api, mode=TradingMode.LIVE, executor=ex, live_enabled=True, clock=clock)
    assert c.get_balances()["USDT"] == 42.5
    assert c.get_wallet()["address"] == ADDR and c.wallet_address == ADDR
    assert "fetched_at" in c.get_wallet_settings()
    assert callable(getattr(c, "get_token_status", None)) and callable(getattr(c, "quote_or_reason", None))


def test_paper_client_has_no_wallet_methods(api):
    from backend.exchange.live import BinanceWeb3Client
    c = BinanceWeb3Client(api, mode=TradingMode.PAPER)
    assert getattr(c, "get_wallet", None) is None and getattr(c, "get_wallet_settings", None) is None


def test_factory_live_uses_baw_live_client(monkeypatch, tmp_path):
    from backend.exchange.factory import get_exchange_client
    for k, v in {**GATE_ON, "BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s", "BINANCE_PUBLIC_STATUS": "false"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("BINANCE_WALLET_ADDRESS", raising=False)
    c = get_exchange_client(TradingMode.LIVE, executor=ex_for(FakeBaw(), tmp_path))
    assert isinstance(c, BawLiveClient) and c.wallet_address == ADDR and c.max_live_notional == 50


def test_smoke_is_read_only_and_never_swaps(capsys):
    fake = FakeBaw(market_order_swap=AssertionError("smoke must never swap"))
    rc = baw_smoke.main([], cli=BawCli(runner=fake, cmd=["baw"], read_only=True))
    assert rc == 0
    assert "market-order swap" not in fake.cmds()
    assert set(fake.cmds()) <= {"wallet status", "wallet address", "wallet balance", "wallet settings",
                                "wallet left-quota", "wallet tx-lock", "market-order quote"}
    q = fake.argv("market-order quote")
    assert q[q.index("--fromTokenQty") + 1] == "1" and q[q.index("--toToken") + 1] == baw_smoke.NVDAB_BSC
    assert q[q.index("--slippage") + 1] == "1"
    assert "ALL READ-ONLY CHECKS PASSED" in capsys.readouterr().out


def test_smoke_refuses_writable_client():
    with pytest.raises(ValueError):
        baw_smoke.run_checks(BawCli(runner=FakeBaw(), cmd=["baw"]))
