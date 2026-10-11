import json
import threading

import pytest

from backend.contracts import ExecutionPath, OrderRequest, OrderSide, OrderStatus, Quote, TradingMode
from backend.exchange import baw as B
from backend.exchange.baw import BawCli
from backend.exchange.baw_executor import BawExecutor, IntentStore, live_gate, result_reason
from backend.exchange.executors import RouteContext

from ._baw_fakes import NVDAB, NVDAON, ORDER_ID, TX, USDT, FakeBaw, err, listing, ok, order_row, timeout, tx_item

GATE_ON = {"LIVE_TRADING_ENABLED": "true", "BINANCE_LIVE_ENABLED": "true", "RISK_ALLOW_LIVE": "true"}


class Mono:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def make(fake, tmp_path, env=None, **kw):
    mono = Mono()
    ex = BawExecutor(cli=BawCli(runner=fake, cmd=["baw"]), intents=IntentStore(tmp_path / "intents.json"),
                     env=GATE_ON if env is None else env, sleep=mono.sleep, monotonic=mono, lock=threading.Lock(), **kw)
    return ex


def buy(symbol="NVDAB", usdt=10.0):
    order = OrderRequest(symbol=symbol, side=OrderSide.BUY, notional=usdt, mode=TradingMode.LIVE)
    quote = Quote(symbol=symbol, side=OrderSide.BUY, notional=usdt, quantity=0.054, price=usdt / 0.054, fee=0.01)
    ctx = RouteContext(chain_id="56", from_token=USDT, to_token=NVDAB if symbol == "NVDAB" else NVDAON,
                       from_decimals=18, to_decimals=18, amount=str(int(usdt * 1e18)), from_qty=usdt)
    return order, quote, ctx


def run(ex, *args):
    order, quote, ctx = args or buy()
    return ex.execute(order, quote, ctx, ExecutionPath.SWAP)


def test_happy_buy_real_fill_gas_and_extras(tmp_path):
    fake = FakeBaw()
    seen = {}

    def swap(argv):
        seen["intent"] = json.loads((tmp_path / "intents.json").read_text())  # persisted BEFORE submitting
        return ok({"orderId": ORDER_ID, "clientOrderId": "c"})
    fake.set("market-order swap", swap)
    ex = make(fake, tmp_path)
    ex.get_balances()  # caches BNB price 600 for gas in USD
    r = run(ex)
    assert r.status == OrderStatus.FILLED and r.mode == TradingMode.LIVE
    assert r.tx_hash == TX and r.order_id == ORDER_ID
    assert r.filled_quantity == pytest.approx(0.0539)  # from tx-history receive, not the quote/estimate
    assert r.notional == pytest.approx(10.0) and r.avg_price == pytest.approx(10 / 0.0539)
    assert r.fee == pytest.approx(0.01 + 52e-6 * 600)
    (intent,) = seen["intent"].values()
    assert intent["state"] == "SUBMITTING" and intent["from_qty"] == "10"
    assert ex.intents.get(r.client_order_id)["state"] == "FINISHED"
    x = ex.extras[r.client_order_id]
    assert x["explorer_url"] == f"https://bscscan.com/tx/{TX}" and x["real_funds"] is True
    assert x["route"] == "SWAP" and x["wallet_order_id"] == ORDER_ID and x["gas_fee_bnb"] == pytest.approx(52e-6)
    assert fake.cmds()[:4] == ["wallet balance", "wallet status", "wallet tx-lock", "market-order list"]
    assert fake.cmds().count("market-order swap") == 1


def test_swap_argv_has_explicit_slippage_1_by_default(tmp_path):
    fake = FakeBaw()
    run(make(fake, tmp_path))
    argv = fake.argv("market-order swap")
    assert argv[argv.index("--slippage") + 1] == "1"
    assert argv[argv.index("--mev") + 1] == "true"
    assert "auto" not in argv
    assert argv[argv.index("--fromToken") + 1] == USDT and argv[argv.index("--toToken") + 1] == NVDAB


def test_slippage_env_and_per_call_override(tmp_path):
    fake = FakeBaw()
    ex = make(fake, tmp_path, env={**GATE_ON, "BINANCE_LIVE_MAX_SLIPPAGE_PCT": "0.7"})
    run(ex)
    a = fake.argv("market-order swap")
    assert a[a.index("--slippage") + 1] == "0.7"
    fake2 = FakeBaw()
    ex2 = make(fake2, tmp_path / "b")
    order, quote, ctx = buy()
    ex2.execute_order(order, USDT, NVDAB, 10, 10, quote=quote, max_slippage_pct=0.3)
    a = fake2.argv("market-order swap")
    assert a[a.index("--slippage") + 1] == "0.3"


@pytest.mark.parametrize("missing", ["LIVE_TRADING_ENABLED", "BINANCE_LIVE_ENABLED", "RISK_ALLOW_LIVE"])
def test_gate_refuses_without_any_baw_call(tmp_path, missing):
    fake = FakeBaw()
    r = run(make(fake, tmp_path, env={**GATE_ON, missing: "false"}))
    assert r.status == OrderStatus.REJECTED and result_reason(r) == B.LIVE_DISABLED and missing in r.error
    assert fake.calls == []


def test_gate_helper():
    assert live_gate(GATE_ON) == (True, [])
    assert live_gate({}, require_risk_allow_live=False)[1] == ["LIVE_TRADING_ENABLED=true", "BINANCE_LIVE_ENABLED=true"]


def test_per_order_hard_cap_default_25(tmp_path):
    fake = FakeBaw()
    r = run(make(fake, tmp_path), *buy(usdt=25.01))
    assert result_reason(r) == B.ORDER_CAP_EXCEEDED and "BINANCE_LIVE_MAX_ORDER_USDT" in r.error
    assert fake.calls == []
    r = run(make(FakeBaw(), tmp_path / "b", env={**GATE_ON, "BINANCE_LIVE_MAX_ORDER_USDT": "5"}), *buy(usdt=6))
    assert result_reason(r) == B.ORDER_CAP_EXCEEDED


def test_non_live_order_refused(tmp_path):
    order, quote, ctx = buy()
    order = order.model_copy(update={"mode": TradingMode.PAPER})
    assert result_reason(run(make(FakeBaw(), tmp_path), order, quote, ctx)) == B.LIVE_DISABLED


def test_tx_locked_blocks_swap(tmp_path):
    fake = FakeBaw(wallet_tx_lock=ok({"status": "LOCKED"}))
    r = run(make(fake, tmp_path))
    assert r.status == OrderStatus.REJECTED and result_reason(r) == B.TX_LOCKED
    assert "market-order swap" not in fake.cmds()


def test_pending_wallet_order_blocks_swap(tmp_path):
    fake = FakeBaw(market_order_list=lambda argv: ok(listing(order_row("PENDING", order_id="99"))))
    r = run(make(fake, tmp_path))
    assert result_reason(r) == B.ORDER_IN_FLIGHT and "market-order swap" not in fake.cmds()
    assert fake.argv("market-order list")[3:5] == ["--pageSize", "20"] and "--status" in fake.argv("market-order list")


@pytest.mark.parametrize("status_resp", [ok({"status": "UNCONNECTED"}), err(10003002, "SESSION_EXPIRED", "Session expired")])
def test_session_expired_is_clear_reason(tmp_path, status_resp):
    fake = FakeBaw(wallet_status=status_resp)
    r = run(make(fake, tmp_path))
    assert r.status == OrderStatus.REJECTED and result_reason(r) == B.SESSION_EXPIRED
    assert result_reason(r) in B.PAUSE_REASONS
    assert "market-order swap" not in fake.cmds()


def test_process_lock_single_flight(tmp_path):
    fake = FakeBaw()
    ex = make(fake, tmp_path)
    ex._lock.acquire()
    try:
        r = run(ex)
    finally:
        ex._lock.release()
    assert result_reason(r) == B.ORDER_IN_FLIGHT and fake.calls == []


def test_swap_timeout_reconciles_instead_of_retrying(tmp_path):
    fake = FakeBaw(market_order_swap=timeout())
    fake.set("market-order list", lambda argv: ok(listing(order_row()) if ("--orderId" in argv or "--fromToken" in argv)
                                                  else listing()))
    r = run(make(fake, tmp_path))
    assert r.status == OrderStatus.FILLED and r.order_id == ORDER_ID
    assert fake.cmds().count("market-order swap") == 1
    found = next(c for c in fake.calls if "--fromToken" in c and c[2] == "list")
    assert "--startTime" in found


def test_swap_timeout_unknown_outcome_keeps_intent_and_blocks(tmp_path):
    fake = FakeBaw(market_order_swap=timeout())
    ex = make(fake, tmp_path)
    r = run(ex)
    assert r.status == OrderStatus.PENDING_CONFIRMATION and result_reason(r) == B.OUTCOME_UNKNOWN
    assert fake.cmds().count("market-order swap") == 1
    r2 = run(ex)
    assert result_reason(r2) == B.ORDER_IN_FLIGHT and fake.cmds().count("market-order swap") == 1
    ex.resolve_intent(r.client_order_id, "FAILED", "checked App: no order")
    fake.set("market-order swap", ok({"orderId": ORDER_ID}))
    assert run(ex).status == OrderStatus.FILLED


def test_still_pending_after_poll_window(tmp_path):
    fake = FakeBaw(market_order_list=lambda argv: ok(listing(order_row("PENDING", tx=None)) if "--orderId" in argv else listing()))
    ex = make(fake, tmp_path)
    r = run(ex)
    assert r.status == OrderStatus.PENDING_CONFIRMATION and r.order_id == ORDER_ID
    assert result_reason(r) == B.PENDING_CONFIRMATION
    polls = [c for c in fake.calls if "--orderId" in c]
    assert 30 <= len(polls) <= 42  # every 3 s for 120 s
    assert ex.intents.get(r.client_order_id)["state"] == "PENDING"
    # later tick: order finished -> reconcile closes the intent
    fake.set("market-order list", lambda argv: ok(listing(order_row())))
    (rec,) = ex.reconcile()
    assert rec["state"] == "FINISHED" and rec["tx_hash"] == TX


def test_poll_survives_read_timeouts(tmp_path):
    seq = [timeout(), ok(listing(order_row("PENDING"))), ok(listing(order_row()))]
    fake = FakeBaw()
    fake.set("market-order list", lambda argv: (seq.pop(0) if seq else ok(listing(order_row()))) if "--orderId" in argv else ok(listing()))
    r = run(make(fake, tmp_path))
    assert r.status == OrderStatus.FILLED


def test_failed_order(tmp_path):
    fake = FakeBaw(market_order_list=lambda argv: ok(listing(order_row("FAILED"))) if "--orderId" in argv else ok(listing()))
    r = run(make(fake, tmp_path))
    assert r.status == OrderStatus.FAILED and result_reason(r) == B.ORDER_FAILED and r.tx_hash == TX


@pytest.mark.parametrize("resp,status,reason", [
    (err(30003001, "ORDER_API_ERROR", "Exceeds daily limit"), OrderStatus.REJECTED, B.QUOTA_EXCEEDED),
    (err(30003001, "ORDER_API_ERROR", "Token not supported by your wallet settings"), OrderStatus.REJECTED, B.POLICY_REJECTED),
    (err(30003001, "ORDER_API_ERROR", "High risk transaction rejected (abnormal price impact)"), OrderStatus.REJECTED, B.ABNORMAL_TXN_REJECTED),
    (err(30003002, "SLIPPAGE_EXCEEDED", "Slippage exceeded"), OrderStatus.REJECTED, B.SLIPPAGE_EXCEEDED),
    (err(30003001, "ORDER_API_ERROR", "execution reverted: min receive amount not met"), OrderStatus.REJECTED, B.SLIPPAGE_EXCEEDED),
    (err(20002002, "INSUFFICIENT_BALANCE", "Insufficient balance"), OrderStatus.REJECTED, B.INSUFFICIENT_BALANCE),
    (err(30002002, "QUOTE_EXPIRED", "Quote expired"), OrderStatus.EXPIRED, B.QUOTE_EXPIRED),
    (err(30003005, "BROADCAST_FAILED", "Broadcast failed"), OrderStatus.FAILED, B.ORDER_FAILED),
    (err(30001004, "APP_CONFIRMATION_REQUIRED", "Confirmation required on App"), OrderStatus.PENDING_CONFIRMATION, B.APP_CONFIRMATION_REQUIRED),
    (err(10003002, "SESSION_EXPIRED", "Session expired"), OrderStatus.REJECTED, B.SESSION_EXPIRED),
])
def test_submit_rejections_are_structured(tmp_path, resp, status, reason):
    fake = FakeBaw(market_order_swap=resp)
    ex = make(fake, tmp_path)
    r = run(ex)
    assert r.status == status and result_reason(r) == reason
    assert ex.extras[r.client_order_id]["reason"] == reason
    assert fake.cmds().count("market-order swap") == 1
    if status in (OrderStatus.REJECTED, OrderStatus.EXPIRED, OrderStatus.FAILED):
        assert ex.intents.open_intents() == []  # definite rejection: nothing in flight


def test_order_api_error_with_order_id_is_followed(tmp_path):
    fake = FakeBaw(market_order_swap=err(30003001, "ORDER_API_ERROR", "Order processing", {"orderId": ORDER_ID}))
    r = run(make(fake, tmp_path))
    assert r.status == OrderStatus.FILLED and r.order_id == ORDER_ID


def test_sell_uses_order_quantity_and_tx_history_send(tmp_path):
    fake = FakeBaw(wallet_tx_history=ok({"transactions": [tx_item(NVDAB, "0.05", USDT, "9.31")], "hasMore": False}))
    fake.set("market-order list", lambda argv: ok(listing(order_row(from_token=NVDAB, to_token=USDT, from_qty="0.05", to_qty=None)))
             if "--orderId" in argv else ok(listing()))
    ex = make(fake, tmp_path)
    order = OrderRequest(symbol="NVDAB", side=OrderSide.SELL, notional=9.3, quantity=0.05, mode=TradingMode.LIVE)
    quote = Quote(symbol="NVDAB", side=OrderSide.SELL, notional=9.3, quantity=0.05, price=186)
    ctx = RouteContext(chain_id="56", from_token=NVDAB, to_token=USDT, from_decimals=18, to_decimals=18, amount="1",
                       from_qty=0.0499)
    r = ex.execute(order, quote, ctx, ExecutionPath.SWAP)
    a = fake.argv("market-order swap")
    assert a[a.index("--fromTokenQty") + 1] == "0.05"
    assert r.status == OrderStatus.FILLED and r.filled_quantity == pytest.approx(0.05)
    assert r.notional == pytest.approx(9.31)


def test_ondo_route_tag(tmp_path):
    ex = make(FakeBaw(), tmp_path)
    r = run(ex, *buy(symbol="NVDAon"))
    assert ex.extras[r.client_order_id]["route"] == "ONDO_RFQ"


def test_failed_row_with_slippage_message(tmp_path):
    row = {**order_row("FAILED"), "failReason": "Swap failed: slippage too high"}
    fake = FakeBaw(market_order_list=lambda argv: ok(listing(row)) if "--orderId" in argv else ok(listing()))
    r = run(make(fake, tmp_path))
    assert r.status == OrderStatus.FAILED and result_reason(r) == B.SLIPPAGE_EXCEEDED


def test_execute_signature_accepts_max_slippage_pct():
    import inspect
    from backend.exchange.baw_executor import BawExecutor
    assert "max_slippage_pct" in inspect.signature(BawExecutor.execute).parameters


def test_execute_passes_per_call_slippage(tmp_path):
    fake = FakeBaw()
    order, quote, ctx = buy()
    make(fake, tmp_path).execute(order, quote, ctx, ExecutionPath.SWAP, max_slippage_pct=0.4)
    a = fake.argv("market-order swap")
    assert a[a.index("--slippage") + 1] == "0.4"
