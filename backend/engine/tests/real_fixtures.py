"""Hand-built fixtures shaped like real Binance Web3 ``market/candles`` data for tokenized stocks.

No real responses are saved on the box yet (Binance blocks the box's US IP with 40304; Bot 4 will save samples to
/workspace/binance-check/samples/ from Rishu's PC). These mimic what Bot 4 documented
(/workspace/teamwork-bot4-research/binance-research.md section 7): rows ``[open, high, low, close, volume, ts_ms,
tradeCount]`` as STRINGS, hourly bars SPARSE off-hours / weekends, DEX volume often 0, NVDAon ~230 USDT.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from backend.contracts import Candle

MONDAY = datetime(2026, 10, 5, tzinfo=timezone.utc)  # a Monday
HOUR = timedelta(hours=1)


def rows_to_candles(rows: Iterable[list]) -> list[Candle]:
    """Same parsing Bot 4's BinanceWeb3Client.get_candles does (skip malformed rows, widen high/low)."""
    out = []
    for r in rows:
        try:
            o, h, lo, c, v, ts = float(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), int(float(r[5]))
            out.append(
                Candle(
                    timestamp=datetime.fromtimestamp(ts / 1000, tz=timezone.utc),
                    open=o,
                    high=max(h, o, c),
                    low=min(lo, o, c),
                    close=c,
                    volume=max(v, 0.0),
                )
            )
        except (IndexError, TypeError, ValueError):
            continue
    out.sort(key=lambda k: k.timestamp)
    return out


def session_hours_1h(days: int, start: datetime = MONDAY, hours: Iterable[int] = range(13, 20)) -> list[datetime]:
    """Bar open times for weekdays only, within ``hours`` (UTC). Weekends produce no bars at all."""
    hs = sorted(set(hours))
    out = []
    for d in range(days):
        day = start + timedelta(days=d)
        if day.weekday() >= 5:
            continue
        out.extend(day + timedelta(hours=h) for h in hs)
    return out


def all_hours_1h(days: int, start: datetime = MONDAY) -> list[datetime]:
    """24/7 hourly bar open times (on-chain tokens can trade around the clock, incl. weekends)."""
    return [start + k * HOUR for k in range(days * 24)]


def is_us_session(t: datetime) -> bool:
    return t.weekday() < 5 and 13 <= t.hour < 20


def real_shaped_rows(
    times: list[datetime],
    price: float = 230.88,
    drift: float = 0.0,
    noise: float = 0.003,
    seed: int = 7,
    zero_volume_prob: float = 0.3,
    missing_prob: float = 0.1,
    gap_jump: float = 0.0,
    gap_after: timedelta = timedelta(days=1),
    off_hours_zero_volume_prob: Optional[float] = None,
    off_hours_volume_scale: float = 1.0,
) -> list[list[str]]:
    """String rows like the API returns. Random bars are missing, ~30% have 0 DEX volume, and the first bar after a
    gap longer than ``gap_after`` (default: a day, i.e. weekends) opens ``gap_jump`` away from the previous close;
    the jump alternates in sign so gaps add jumps but no net trend. For 24/7 data, bars outside the US session can
    get their own zero-volume probability and a smaller volume scale (thin overnight / weekend liquidity)."""
    rng = random.Random(seed)
    rows: list[list[str]] = []
    prev_t: Optional[datetime] = None
    p = price
    sign = 1.0
    for t in times:
        if rng.random() < missing_prob:
            continue
        o = p
        if prev_t is not None and (t - prev_t) > gap_after:
            o = p * (1 + sign * gap_jump)
            sign = -sign
        c = o * (1 + drift + rng.gauss(0, noise))
        h = max(o, c) * (1 + abs(rng.gauss(0, noise / 2)))
        lo = min(o, c) * (1 - abs(rng.gauss(0, noise / 2)))
        off = off_hours_zero_volume_prob is not None and not is_us_session(t)
        zp = off_hours_zero_volume_prob if off else zero_volume_prob
        scale = off_hours_volume_scale if off else 1.0
        v = 0.0 if rng.random() < zp else round(rng.uniform(0.05, 3.0) * scale, 6)
        n = 0 if v == 0 else rng.randint(1, 40)
        rows.append([f"{o:.8f}", f"{h:.8f}", f"{lo:.8f}", f"{c:.8f}", f"{v}", str(int(t.timestamp() * 1000)), str(n)])
        p, prev_t = c, t
    return rows


def real_shaped_candles(days: int = 40, **kw) -> list[Candle]:
    return rows_to_candles(real_shaped_rows(session_hours_1h(days), **kw))


def round_the_clock_candles(days: int = 21, **kw) -> list[Candle]:
    """24/7 hourly bars: liquid in the US session, mostly empty / tiny volume overnight and at weekends."""
    kw.setdefault("off_hours_zero_volume_prob", 0.8)
    kw.setdefault("off_hours_volume_scale", 0.05)
    kw.setdefault("missing_prob", 0.0)
    return rows_to_candles(real_shaped_rows(all_hours_1h(days), **kw))


def dead_bars(after: Candle, n: int) -> list[Candle]:
    """Placeholder bars of a closed market: flat at the last close, no volume."""
    p = after.close
    return [
        Candle(timestamp=after.timestamp + (k + 1) * HOUR, open=p, high=p, low=p, close=p, volume=0) for k in range(n)
    ]
