import math

import pandas as pd
import pytest

from backend.engine import indicators as ind
from backend.mocks import generate_candles


def approx_list(a, b, tol=1e-9):
    assert len(a) == len(b)
    for x, y in zip(a, b):
        if y is None:
            assert x is None
        else:
            assert x == pytest.approx(y, abs=tol)


def test_sma_hand_computed():
    approx_list(ind.sma([1, 2, 3, 4, 5], 3), [None, None, 2, 3, 4])


def test_ema_sma_seed_hand_computed():
    # seed = mean(1,2,3) = 2 at index 2; k = 0.5 -> 3, 4
    approx_list(ind.ema([1, 2, 3, 4, 5], 3), [None, None, 2, 3, 4])
    # k = 2/(4+1) = 0.4; seed mean(2,4,6,8) = 5; next = 10*0.4 + 5*0.6 = 7
    approx_list(ind.ema([2, 4, 6, 8, 10], 4), [None, None, None, 5, 7])


def test_ema_matches_pandas_after_seed():
    closes = [c.close for c in generate_candles(n=120, seed=3)]
    n = 10
    ours = ind.ema(closes, n)
    seed = sum(closes[:n]) / n
    ref = pd.Series([seed] + closes[n:]).ewm(span=n, adjust=False).mean().tolist()
    approx_list(ours[n - 1:], ref, 1e-9)


def test_rsi_wilder_hand_computed():
    # changes +1, -1, +1 ; period 2: seed avg_g = avg_l = 0.5 -> 50 ; then g = 0.75, l = 0.25 -> RS 3 -> 75
    approx_list(ind.rsi([1, 2, 1, 2], 2), [None, None, 50.0, 75.0])


def test_rsi_extremes_and_flat():
    assert ind.rsi(list(range(1, 30)), 14)[-1] == 100.0  # avg loss 0 -> 100
    assert ind.rsi(list(range(30, 1, -1)), 14)[-1] == 0.0
    assert ind.rsi([5.0] * 30, 14)[-1] == 50.0  # no movement at all -> neutral
    assert ind.rsi([1, 2, 3], 14) == [None, None, None]


def test_rsi_matches_independent_pandas_wilder():
    closes = [c.close for c in generate_candles(n=200, seed=11)]
    p = 14
    ch = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    g = [max(x, 0) for x in ch]
    l_ = [max(-x, 0) for x in ch]
    ag = pd.Series([sum(g[:p]) / p] + g[p:]).ewm(alpha=1 / p, adjust=False).mean()
    al = pd.Series([sum(l_[:p]) / p] + l_[p:]).ewm(alpha=1 / p, adjust=False).mean()
    ref = (100 - 100 / (1 + ag / al)).tolist()
    approx_list(ind.rsi(closes, p)[p:], ref, 1e-9)


def test_simulator_rsi_differs_after_seed():
    """Documented deviation: the simulator smooths SUMS as if they were averages."""
    closes = [c.close for c in generate_candles(n=100, seed=5)]
    ours, sim = ind.rsi(closes), ind.rsi_simulator(closes)
    assert ours[14] == pytest.approx(sim[14])  # same ratio at the seed bar
    assert abs(ours[15] - sim[15]) > 0.5  # diverges right after


def test_macd_true_nine_period_signal():
    closes = [c.close for c in generate_candles(n=150, seed=8)]
    line, sig, hist = ind.macd(closes, 12, 26, 9)
    first_line = 25
    assert line[first_line - 1] is None and line[first_line] is not None
    seed_idx = first_line + 8
    assert sig[seed_idx - 1] is None
    assert sig[seed_idx] == pytest.approx(sum(line[first_line:seed_idx + 1]) / 9)
    for i in range(seed_idx + 1, len(closes)):
        assert sig[i] == pytest.approx(0.2 * line[i] + 0.8 * sig[i - 1])  # alpha = 2/(9+1)
        assert hist[i] == pytest.approx(line[i] - sig[i])
    e12, e26 = ind.ema(closes, 12), ind.ema(closes, 26)
    assert line[-1] == pytest.approx(e12[-1] - e26[-1])


def test_macd_zero_values_are_not_treated_as_missing():
    line, sig, _ = ind.macd([10.0] * 60)
    assert line[40] == 0.0 and sig[40] == 0.0
    assert ind.crossed_above(line, sig, 40) is False  # a real boolean, not None (no truthiness bug)
    assert ind.crossed_below(line, sig, 40) is False


def test_macd_rejects_bad_periods():
    with pytest.raises(ValueError):
        ind.macd([1.0] * 50, 26, 12)


def test_bollinger_population_std():
    lower, mid, upper = ind.bollinger([1, 2, 3, 4, 5], 5, 2.0)
    assert lower[:4] == [None] * 4
    assert mid[4] == 3
    assert lower[4] == pytest.approx(3 - 2 * math.sqrt(2))
    assert upper[4] == pytest.approx(3 + 2 * math.sqrt(2))


def test_volume_average_rolling_no_lookahead():
    vols = [100.0] * 30
    a = ind.volume_average(vols, 20)
    vols2 = vols[:25] + [1e9] * 5
    b = ind.volume_average(vols2, 20)
    assert a[:25] == b[:25]  # future spikes don't change the past
    assert a[18] is None and a[19] == 100.0
    assert ind.volume_average([0.0, 0.0, 0.0], 2) == [None, 0.0, 0.0]
    # simulator mode: one whole-series average
    assert ind.volume_average([1.0, 3.0], None) == [2.0, 2.0]
    assert ind.volume_average([], None) == []


def test_atr_and_rolling_extremes():
    h, lo, c = [2, 3, 4, 5], [1, 1, 2, 3], [1.5, 2.5, 3.5, 4.5]
    a = ind.atr(h, lo, c, 2)
    # TRs: max(2, 1.5, 0.5)=2 ; max(2, 1.5, 0.5)=2 ; max(2, 1.5, 0.5)=2
    assert a == [None, None, 2.0, 2.0]
    assert ind.rolling_max([1, 5, 2, 3], 2) == [None, None, 5, 5]
    assert ind.rolling_max([1, 5, 2, 3], 2, include_current=True) == [None, 5, 5, 3]
    assert ind.rolling_min([4, 1, 2, 3], 2) == [None, None, 1, 1]


def test_crossovers():
    a, b = [1.0, 2.0, 1.0], [1.5, 1.5, 1.5]
    assert ind.crossed_above(a, b, 1) is True
    assert ind.crossed_below(a, b, 2) is True
    assert ind.crossed_above(a, b, 0) is None
    assert ind.crossed_above([None, 2.0], b, 1) is None


def test_period_validation():
    with pytest.raises(ValueError):
        ind.sma([1.0], 0)
