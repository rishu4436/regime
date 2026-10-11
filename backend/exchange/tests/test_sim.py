from datetime import datetime, timezone

from backend.contracts import ExchangeClient, ExecutionPath, OrderRequest, OrderSide, OrderStatus, TokenType, TradingMode
from backend.exchange.sim import SimulatedBinanceClient


def order(sym, side=OrderSide.BUY, notional=100.0, **kw):
    return OrderRequest(symbol=sym, side=side, notional=notional, **kw)


def test_protocol_and_tokens_have_real_contracts(clock):
    c = SimulatedBinanceClient(clock=clock)
    assert isinstance(c, ExchangeClient) and c.mode == TradingMode.SIM
    toks = {t.symbol: t for t in c.search_tokens("")}
    assert set(toks) == {"NVDAon", "TSLAx", "AAPLB", "SPYon", "METAx"}
    assert toks["NVDAon"].chain_id == "56" and toks["NVDAon"].contract_address.startswith("0xa9ee")
    assert all(t.price and t.price > 0 for t in toks.values())
    assert [t.symbol for t in c.search_tokens("tesla")] == ["TSLAx"]


def test_routing_by_issuer(clock):
    c = SimulatedBinanceClient(clock=clock)
    assert c.get_quote("NVDAon", OrderSide.BUY, 100).path == ExecutionPath.RFQ
    # xStock quotes are RFQ on the real API (40001 "userWalletAddress is required for RFQ (xStock) quote")
    assert c.get_quote("TSLAx", OrderSide.BUY, 100).path == ExecutionPath.RFQ
    assert c.get_quote("METAx", OrderSide.BUY, 100).path == ExecutionPath.RFQ
    assert c.get_quote("AAPLB", OrderSide.BUY, 100).path == ExecutionPath.SWAP


def test_quote_ttl_30s_and_expiry(clock):
    c = SimulatedBinanceClient(clock=clock)
    q = c.get_quote("AAPLB", OrderSide.BUY, 100)
    assert (q.expires_at - q.created_at).total_seconds() == 30
    clock.advance(31)
    assert c.execute_swap(order("AAPLB"), q).status == OrderStatus.EXPIRED


def test_fill_swap_and_rfq(clock):
    c = SimulatedBinanceClient(clock=clock)
    q = c.get_quote("AAPLB", OrderSide.BUY, 100)
    r = c.execute_swap(order("AAPLB"), q)
    assert r.status == OrderStatus.FILLED and r.path == ExecutionPath.SWAP and r.tx_hash.startswith("0xsim")
    assert abs(r.filled_quantity - q.quantity) < 1e-12
    q2 = c.get_quote("NVDAon", OrderSide.BUY, 100)
    assert c.execute_rfq(order("NVDAon"), q2).status == OrderStatus.FILLED
    q3 = c.get_quote("TSLAx", OrderSide.BUY, 100)
    assert c.execute_rfq(order("TSLAx"), q3).status == OrderStatus.FILLED
    # a quote can only be used once
    assert c.execute_rfq(order("NVDAon"), q2).status == OrderStatus.REJECTED


def test_wrong_path_rejected(clock):
    c = SimulatedBinanceClient(clock=clock)
    q = c.get_quote("NVDAon", OrderSide.BUY, 100)
    r = c.execute_swap(order("NVDAon"), q)
    assert r.status == OrderStatus.REJECTED and "execute_rfq" in r.error
    q = c.get_quote("TSLAx", OrderSide.BUY, 100)
    r = c.execute_swap(order("TSLAx"), q)
    assert r.status == OrderStatus.REJECTED and "execute_rfq" in r.error


def test_ondo_minimum_order(clock):
    c = SimulatedBinanceClient(clock=clock)
    q = c.get_quote("SPYon", OrderSide.BUY, 10)
    r = c.execute_rfq(order("SPYon", notional=10), q)
    assert r.status == OrderStatus.REJECTED and "40375" in r.error


def test_market_hours_gate_optional():
    sat = datetime(2026, 10, 10, 15, 0, tzinfo=timezone.utc)
    c = SimulatedBinanceClient(clock=lambda: sat, enforce_market_hours=True)
    r = c.execute_rfq(order("NVDAon"), c.get_quote("NVDAon", OrderSide.BUY, 100))
    assert r.status == OrderStatus.REJECTED and "40367" in r.error
    # xStock RFQ has no liquidity out of hours (a Sunday TSLAx quote returned 40374)
    r = c.execute_rfq(order("TSLAx"), c.get_quote("TSLAx", OrderSide.BUY, 100))
    assert r.status == OrderStatus.REJECTED and "40374" in r.error
    r = c.execute_swap(order("AAPLB"), c.get_quote("AAPLB", OrderSide.BUY, 100))
    assert r.status == OrderStatus.REJECTED and "40369" in r.error
    mon = datetime(2026, 10, 12, 15, 0, tzinfo=timezone.utc)
    c2 = SimulatedBinanceClient(clock=lambda: mon, enforce_market_hours=True)
    assert c2.execute_rfq(order("TSLAx"), c2.get_quote("TSLAx", OrderSide.BUY, 100)).status == OrderStatus.FILLED


def test_refuses_live(clock):
    c = SimulatedBinanceClient(clock=clock)
    q = c.get_quote("AAPLB", OrderSide.BUY, 100)
    assert c.execute_swap(order("AAPLB", mode=TradingMode.LIVE), q).status == OrderStatus.REJECTED
    try:
        SimulatedBinanceClient(mode=TradingMode.LIVE)
    except ValueError:
        pass
    else:
        raise AssertionError("LIVE must be refused")


def test_sell_with_quantity(clock):
    c = SimulatedBinanceClient(clock=clock)
    q = c.get_quote("AAPLB", OrderSide.SELL, 100)
    r = c.execute_swap(order("AAPLB", side=OrderSide.SELL, quantity=0.25), q)
    assert r.status == OrderStatus.FILLED and r.filled_quantity == 0.25


def test_deterministic_with_mocks(clock):
    from backend.mocks import MockExchangeClient

    assert SimulatedBinanceClient(clock=clock).get_price("NVDAon") == MockExchangeClient().get_price("NVDAon")
    assert {t.token_type for t in SimulatedBinanceClient().search_tokens("")} >= {TokenType.ONDO, TokenType.XSTOCK}
