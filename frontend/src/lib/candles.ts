// Deterministic mock candles for the mock API (mirrors backend/mocks/candles.py shapes; the RNG differs,
// so values are plausible but not byte-identical to the Python mocks).
import type { Candle, Regime, Timeframe } from "../contracts";

const TF_MS: Record<Timeframe, number> = {
  "1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000,
};

// per-bar drift and noise per regime (same as backend/mocks/candles.py)
const SHAPES: Record<Regime, [number, number]> = {
  STRONG_UPTREND: [0.004, 0.003],
  MILD_UPTREND: [0.0015, 0.004],
  SIDEWAYS: [0.0, 0.002],
  OVERSOLD_PULLBACK: [0.001, 0.004],
  HIGH_VOLATILITY: [0.0, 0.03],
  DOWNTREND: [-0.004, 0.004],
};

export const DEFAULT_START = Date.UTC(2026, 0, 5, 0, 0, 0);

/** Seeded PRNG (mulberry32) */
export function rng(seedStr: string): () => number {
  let h = 1779033703 ^ seedStr.length;
  for (let i = 0; i < seedStr.length; i++) {
    h = Math.imul(h ^ seedStr.charCodeAt(i), 3432918353);
    h = (h << 13) | (h >>> 19);
  }
  let a = h >>> 0;
  return () => {
    a |= 0; a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function gauss(r: () => number, sd: number): number {
  const u = Math.max(1e-12, r()), v = r();
  return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v) * sd;
}

export interface CandleOpts {
  regime?: Regime; n?: number; seed?: number; startPrice?: number; timeframe?: Timeframe; startTime?: number;
}

export function generateCandles(o: CandleOpts = {}): Candle[] {
  const regime = o.regime ?? "SIDEWAYS", n = o.n ?? 250, seed = o.seed ?? 42, start = o.startPrice ?? 100;
  const step = TF_MS[o.timeframe ?? "1h"], t0 = o.startTime ?? DEFAULT_START;
  const r = rng(`${seed}:${regime}:${n}:${start}`);
  const [drift, noise] = SHAPES[regime];
  const dropBars = Math.max(8, Math.floor(n / 20));
  const out: Candle[] = [];
  let price = start;
  for (let i = 0; i < n; i++) out.push(nextCandle(i, n, regime, drift, noise, dropBars, price, t0 + i * step, r)), (price = out[i].close);
  return out;
}

function nextCandle(i: number, n: number, regime: Regime, drift: number, noise: number, dropBars: number,
  price: number, ts: number, r: () => number): Candle {
  let d = drift;
  if (regime === "SIDEWAYS") d = 0.004 * Math.sin(i / 6) - 0.004 * Math.sin((i - 1) / 6);
  if (regime === "OVERSOLD_PULLBACK" && i >= n - dropBars) d = -0.02;
  const open = price;
  const close = Math.max(0.01, open * (1 + d + gauss(r, noise)));
  const wick = Math.abs(gauss(r, noise * 0.6)) + 0.0005;
  const baseVol = 5000 * (regime === "HIGH_VOLATILITY" ? 3 : 1);
  const vol = baseVol * (0.5 + r()) * (regime === "OVERSOLD_PULLBACK" && i >= n - dropBars ? 1.8 : 1);
  return {
    timestamp: new Date(ts).toISOString(), open: round(open), high: round(Math.max(open, close) * (1 + wick)),
    low: round(Math.min(open, close) * (1 - wick)), close: round(close), volume: Math.round(vol * 100) / 100,
  };
}

/** Append one random-walk candle (used by the mock live loop). */
export function appendCandle(prev: Candle[], r: () => number, regime: Regime, timeframe: Timeframe = "1h"): Candle {
  const last = prev[prev.length - 1];
  const [drift, noise] = SHAPES[regime];
  return nextCandle(prev.length, Number.MAX_SAFE_INTEGER, regime, drift, noise, 0, last.close,
    Date.parse(last.timestamp) + TF_MS[timeframe], r);
}

const round = (x: number) => Math.round(x * 1e6) / 1e6;
