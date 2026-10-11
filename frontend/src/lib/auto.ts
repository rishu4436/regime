// Auto Mode display helpers + an INDICATIVE regime detector / (regime, style) -> preset map used only by the
// mock API and for previews. The authoritative versions are Bot 2's detect_regime / select_strategy.
import type { Candle, PresetName, Regime, TraderStyle } from "../contracts";
import { ema, rsi, sma } from "./indicators";

export const STYLE_INFO: Record<TraderStyle, { label: string; blurb: string }> = {
  PATIENT: { label: "Patient / Accumulator", blurb: "Builds positions slowly, prefers DCA, sits out chaos." },
  BALANCED: { label: "Balanced / Swing", blurb: "Mixes trend and mean-reversion, moderate trade frequency." },
  ACTIVE: { label: "Active / Scalp-Spot", blurb: "Trades more often, grids ranges and rides momentum." },
};

export const PRESET_INFO: Record<PresetName, { label: string; blurb: string }> = {
  DCA: { label: "DCA", blurb: "Fixed-size buys at a regular interval." },
  SMART_DCA: { label: "Smart DCA", blurb: "DCA that buys more when price is below its average." },
  BUY_THE_DIP: { label: "Buy the Dip", blurb: "Buys sharp oversold pullbacks (low RSI)." },
  TREND_FOLLOWING: { label: "Trend / Momentum", blurb: "Buys strength while price is above rising averages." },
  GRID: { label: "Grid", blurb: "Buys low / sells high inside a sideways range." },
};

export const REGIME_INFO: Record<Regime, { label: string; color: string }> = {
  STRONG_UPTREND: { label: "Strong uptrend", color: "#10b981" },
  MILD_UPTREND: { label: "Mild uptrend", color: "#34d399" },
  SIDEWAYS: { label: "Sideways", color: "#9ca3af" },
  OVERSOLD_PULLBACK: { label: "Oversold pullback", color: "#f59e0b" },
  HIGH_VOLATILITY: { label: "High volatility", color: "#a855f7" },
  DOWNTREND: { label: "Downtrend", color: "#ef4444" },
};

/** Indicative map (spec §3); null = stay out. */
export const STYLE_MAP: Record<Regime, Record<TraderStyle, [PresetName | null, number]>> = {
  STRONG_UPTREND: { PATIENT: ["SMART_DCA", 1], BALANCED: ["TREND_FOLLOWING", 1], ACTIVE: ["TREND_FOLLOWING", 1] },
  MILD_UPTREND: { PATIENT: ["DCA", 1], BALANCED: ["SMART_DCA", 1], ACTIVE: ["TREND_FOLLOWING", 1] },
  SIDEWAYS: { PATIENT: ["DCA", 1], BALANCED: ["GRID", 1], ACTIVE: ["GRID", 1] },
  OVERSOLD_PULLBACK: { PATIENT: ["SMART_DCA", 1], BALANCED: ["BUY_THE_DIP", 1], ACTIVE: ["BUY_THE_DIP", 1] },
  HIGH_VOLATILITY: { PATIENT: [null, 0], BALANCED: ["DCA", 0.5], ACTIVE: ["GRID", 0.5] },
  DOWNTREND: { PATIENT: ["DCA", 0.5], BALANCED: [null, 0], ACTIVE: [null, 0] },
};

export function detectRegime(c: Candle[]): { regime: Regime; reason: string; confidence: number } {
  const close = c.map((x) => x.close), n = close.length - 1;
  const s50 = sma(close, 50), e20 = ema(close, 20), r = rsi(close, 14);
  const px = close[n], s = s50[n] ?? px, sPrev = s50[n - 10] ?? s, e = e20[n] ?? px, rv = r[n] ?? 50;
  const slope = ((s - sPrev) / sPrev) * 100;
  const rets = close.slice(-30).map((v, i, a) => (i ? Math.log(v / a[i - 1]) : 0)).slice(1);
  const vol = Math.sqrt(rets.reduce((a, b) => a + b * b, 0) / Math.max(1, rets.length)) * 100;
  const dist = ((px - e) / e) * 100;
  const f = `SMA50 slope ${slope.toFixed(2)}%, RSI ${rv.toFixed(0)}, vol ${vol.toFixed(2)}%/bar, ${dist.toFixed(1)}% vs EMA20`;
  if (vol > 1.8) return { regime: "HIGH_VOLATILITY", reason: `Large swings without direction (${f})`, confidence: 0.75 };
  if (rv < 32 && dist < -2) return { regime: "OVERSOLD_PULLBACK", reason: `Sharp drop with low RSI (${f})`, confidence: 0.7 };
  if (slope < -0.8 && px < s) return { regime: "DOWNTREND", reason: `Price below falling SMA50 (${f})`, confidence: 0.7 };
  if (slope > 2 && px > e) return { regime: "STRONG_UPTREND", reason: `Price well above rising averages (${f})`, confidence: 0.75 };
  if (slope > 0.4 && px > s) return { regime: "MILD_UPTREND", reason: `Price above SMA50, not overextended (${f})`, confidence: 0.65 };
  return { regime: "SIDEWAYS", reason: `Flat averages, contained moves (${f})`, confidence: 0.6 };
}

/** Indicative preset trigger used by the mock loop. */
export function presetWantsBuy(p: PresetName, c: Candle[], barsSinceEntry: number): { buy: boolean; reason: string } {
  const close = c.map((x) => x.close), n = close.length - 1, px = close[n];
  const r = rsi(close, 14)[n] ?? 50, s20 = sma(close, 20)[n] ?? px, s50 = sma(close, 50)[n] ?? px, e20 = ema(close, 20)[n] ?? px;
  switch (p) {
    case "DCA": return barsSinceEntry >= 8 ? { buy: true, reason: "scheduled DCA buy (every 8 bars)" } : { buy: false, reason: `next DCA buy in ${8 - barsSinceEntry} bars` };
    case "SMART_DCA": return barsSinceEntry >= 6 && px < s20 ? { buy: true, reason: `price ${((px / s20 - 1) * 100).toFixed(1)}% below SMA20 - accumulate` } : { buy: false, reason: "price not below its 20-bar average / interval not reached" };
    case "BUY_THE_DIP": return r < 35 ? { buy: true, reason: `RSI ${r.toFixed(0)} < 35 - dip` } : { buy: false, reason: `RSI ${r.toFixed(0)} not oversold` };
    case "TREND_FOLLOWING": return px > e20 && e20 > s50 ? { buy: true, reason: "price > EMA20 > SMA50 - momentum" } : { buy: false, reason: "trend not aligned (price/EMA20/SMA50)" };
    case "GRID": return px < s20 * 0.99 ? { buy: true, reason: "price at lower grid level (SMA20 −1%)" } : { buy: false, reason: "price inside grid, no level touched" };
  }
}
