// Indicators with the reference simulator's semantics (see reference/README.md).
// The mock API uses these so mock backtests match the simulator; the real engine is Bot 2's.
export type Series = (number | null)[];

export function sma(closes: number[], period: number): Series {
  const out: Series = [];
  let sum = 0;
  for (let i = 0; i < closes.length; i++) {
    sum += closes[i];
    if (i >= period) sum -= closes[i - period];
    out.push(i < period - 1 ? null : sum / period);
  }
  return out;
}

export function ema(closes: number[], period: number): Series {
  const k = 2 / (period + 1), out: Series = [];
  let prev: number | null = null;
  for (let i = 0; i < closes.length; i++) {
    if (i < period - 1) { out.push(null); continue; }
    if (prev === null) { let s = 0; for (let j = 0; j < period; j++) s += closes[i - j]; prev = s / period; }
    else prev = closes[i] * k + prev * (1 - k);
    out.push(prev);
  }
  return out;
}

/** Standard Wilder RSI (averages, not the simulator's seeded sums). */
export function rsi(closes: number[], period = 14): Series {
  const out: Series = [null];
  let g = 0, l = 0;
  for (let i = 1; i < closes.length; i++) {
    const ch = closes[i] - closes[i - 1];
    if (i <= period) {
      if (ch > 0) g += ch; else l -= ch;
      if (i === period) { g /= period; l /= period; out.push(l === 0 ? 100 : 100 - 100 / (1 + g / l)); }
      else out.push(null);
    } else {
      g = (g * (period - 1) + Math.max(ch, 0)) / period;
      l = (l * (period - 1) + Math.max(-ch, 0)) / period;
      out.push(l === 0 ? 100 : 100 - 100 / (1 + g / l));
    }
  }
  return out;
}

/** MACD; signal smoothing alpha = 0.15 like the simulator (≈ 9-period). */
export function macd(closes: number[], fast = 12, slow = 26): { line: Series; signal: Series } {
  const ef = ema(closes, fast), es = ema(closes, slow);
  const line: Series = ef.map((v, i) => (v != null && es[i] != null ? v - (es[i] as number) : null));
  const signal: Series = [];
  let prev: number | null = null;
  for (const v of line) {
    if (v == null) { signal.push(null); continue; }
    prev = prev == null ? v : v * 0.15 + prev * 0.85;
    signal.push(prev);
  }
  return { line, signal };
}

export function bollinger(closes: number[], period = 20, k = 2): { upper: Series; lower: Series; mid: Series } {
  const mid = sma(closes, period), upper: Series = [], lower: Series = [];
  for (let i = 0; i < closes.length; i++) {
    const m = mid[i];
    if (m == null) { upper.push(null); lower.push(null); continue; }
    let s = 0; for (let j = 0; j < period; j++) s += (closes[i - j] - m) ** 2;
    const sd = Math.sqrt(s / period);
    upper.push(m + k * sd); lower.push(m - k * sd);
  }
  return { upper, lower, mid };
}

/** Volume average: rolling `period` (look-ahead free) or whole-series mean when period is null. */
export function volumeAvg(volumes: number[], period: number | null | undefined): Series {
  if (!period) { const m = volumes.reduce((a, b) => a + b, 0) / Math.max(1, volumes.length); return volumes.map(() => m); }
  return sma(volumes, period);
}
