from backend.contracts import OrderSide
from backend.exchange.sim import SimulatedBinanceClient
from backend.exchange.tokens import DEFAULT_TOKENS


def test_nvdab_and_tslaon_in_registry():
    syms = {t.symbol for t in DEFAULT_TOKENS}
    assert {"NVDAB", "TSLAon"} <= syms
    nvdab = next(t for t in DEFAULT_TOKENS if t.symbol == "NVDAB")
    assert nvdab.contract_address == "0x02fca66c1d1afb4e2a7884261eb00f63598a7436"
    assert nvdab.chain_id == "56"


def test_sim_serves_nvdab_247():
    sim = SimulatedBinanceClient()
    assert any(t.symbol == "NVDAB" for t in sim.search_tokens("NVDA"))
    assert len(sim.get_candles("NVDAB", limit=100)) == 100
    assert sim.get_price("NVDAB") > 0
    st = sim.get_token_status("NVDAB")
    assert st.trading_status.value == "TRADING" if hasattr(st.trading_status, "value") else st.trading_status == "TRADING"
    res = sim.quote_or_reason("NVDAB", OrderSide.BUY, 20.0)
    assert res.ok, res.reason
