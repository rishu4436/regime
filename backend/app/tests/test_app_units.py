from backend.app.exits import check_protective_exit, exit_reason_of
from backend.app.loop import call_check_exits
from backend.contracts import Action, AgentMode, ExitReason, Portfolio, Signal


def _sig(**kw):
    return Signal(symbol="X", action=Action.SELL, source=AgentMode.MANUAL, strategy="s", **{"reason": "r", **kw})


def test_exit_reason_extraction():
    assert exit_reason_of(_sig(conditions_met=["exit:TAKE_PROFIT"])) == ExitReason.TAKE_PROFIT
    assert exit_reason_of(_sig(conditions_met=["STOP_LOSS"])) == ExitReason.STOP_LOSS
    assert exit_reason_of(_sig(reason="Trailing stop hit")) == ExitReason.TRAILING_STOP
    assert exit_reason_of(_sig(reason="something else")) is None


def test_no_exit_when_untouched():
    assert check_protective_exit(open_=100, high=101, low=99, stop_loss_price=95, take_profit_price=110, trail_level=None) is None


def test_call_check_exits_without_bars_param():
    class Old:
        def check_exits(self, portfolio, prices):
            return [_sig()]

    class New:
        def check_exits(self, portfolio, prices, bars=None):
            assert bars == {"X": 1}
            return []

    pf = Portfolio(starting_capital=1, cash=1, equity=1)
    assert len(call_check_exits(Old(), pf, {}, {"X": 1})) == 1
    assert call_check_exits(New(), pf, {}, {"X": 1}) == []
