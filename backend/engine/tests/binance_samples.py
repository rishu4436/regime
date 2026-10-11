"""Loader for REAL Binance responses saved by Bot 2 on Sun 11 Oct 2026 (~01:47 UTC) from the public skill endpoints
(``/workspace/bot2-notes/samples/``; copied to ``fixtures/binance/``). Public market data only, no keys.

K-line rows are ``[openTime_ms, open, high, low, close, volume, closeTime_ms]`` (strings for prices/volume):
- NVDAB (bStock): 48 contiguous 1h bars, all with real DEX volume (trades 24/7)
- NVDAon (Ondo): 48 contiguous 1h bars, volume "0" on every bar (off-hours, ~0.2% range, price ~= reference)
- TSLAx (xStock): 48 bars spread over 15 Aug -> 4 Oct (practically no trading on BSC)
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend.contracts import Candle

DIR = Path(__file__).parent / "fixtures" / "binance"


def load_json(name: str) -> dict:
    return json.loads((DIR / name).read_text())


def kline_rows(symbol: str) -> list[list]:
    return load_json(f"kline_{symbol}_1h.json")["data"]["klineInfos"]


def kline_candles(symbol: str) -> list[Candle]:
    out = []
    for r in kline_rows(symbol):
        o, h, lo, c, v = (float(x) for x in r[1:6])
        out.append(
            Candle(
                timestamp=datetime.fromtimestamp(int(r[0]) / 1000, tz=timezone.utc),
                open=o,
                high=max(h, o, c),
                low=min(lo, o, c),
                close=c,
                volume=max(v, 0.0),
            )
        )
    return out


def extended(symbol: str, copies: int = 3) -> list[Candle]:
    """The real 48 bars preceded by (copies-1) earlier copies of themselves, shifted back in time and rescaled so
    prices join up. Keeps the real bar shapes / volumes while giving the engine enough history (>= 61 bars)."""
    real = kline_candles(symbol)
    span = real[-1].timestamp - real[0].timestamp + timedelta(hours=1)
    out: list[Candle] = []
    for k in range(copies - 1, -1, -1):
        block = [c.model_copy(update={"timestamp": c.timestamp - k * span}) for c in real]
        if out:
            f = out[-1].close / block[0].open
            block = [
                b.model_copy(update={x: getattr(b, x) * f for x in ("open", "high", "low", "close")}) for b in block
            ]
        out.extend(block)
    # rescale so the LAST block is the real one (exact real prices at the end)
    f = real[-1].close / out[-1].close
    return [c.model_copy(update={x: getattr(c, x) * f for x in ("open", "high", "low", "close")}) for c in out]


LAST_BAR = datetime(2026, 10, 11, 1, tzinfo=timezone.utc)  # NVDAB / NVDAon last bar open time
NOW = LAST_BAR + timedelta(minutes=47)  # when the samples were fetched
