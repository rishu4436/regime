// ManualStrategy defaults (== simulator defaults, see reference/README.md) and a client-side evaluator /
// backtester used ONLY by the mock API. The real engine lives in backend/engine (Bot 2).
import {
  DEFAULT_INITIAL_CAPITAL, SIM_WARMUP_BARS,
  type BacktestResult, type BacktestTrade, type Candle, type EquityPoint, type ExitReason, type ManualStrategy,
} from "../contracts";
import { bollinger, ema, macd, rsi, sma, volumeAvg } from "./indicators";

export const TOKENS = [
  { symbol: "NVDAB", label: "NVDAB (NVIDIA bStock, 24/7)", start: 140 },
  { symbol: "NVDAon", label: "NVDAon (NVIDIA Ondo)", start: 140 },
  { symbol: "AAPLB", label: "AAPLB (Apple bStock)", start: 170 },
  { symbol: "TSLAx", label: "TSLAx (Tesla xStock)", start: 200 },
] as const;

type DeepRequired<T> = { [K in keyof T]-?: T[K] extends object | undefined ? DeepRequired<NonNullable<T[K]>> : T[K] };
/** ManualStrategy with every nested field present (what the builder edits). */
export type FullStrategy = DeepRequired<Omit<ManualStrategy, "id" | "created_at">> & { id?: string; created_at?: string };

export function defaultStrategy(): FullStrategy {
  return {
    name: "My strategy",
    symbol: "NVDAB",
    timeframe: "1h",
    entry: {
      logic: "AND",
      rsi_below: { enabled: false, period: 14, threshold: 35 },
      price_above_sma: { enabled: false, period: 50 },
      // Default entry = price above EMA(20) alone: Bot 2 backtest on 9 datasets, 195 trades, +1.25% after fees, 3.85% worst DD.
      price_above_ema: { enabled: true, period: 20 },
      macd_bullish_cross: { enabled: false, params: { fast: 12, slow: 26, signal: 9 } },
      bollinger_lower: { enabled: false, period: 20, std_dev: 2, proximity_pct: 2 },
      volume_above_avg: { enabled: false, period: null as unknown as number, multiplier: 1.1 },
      breakout: { enabled: false, lookback: 20, direction: "HIGH" },
    },
    exit: {
      take_profit_pct: 12,
      stop_loss_pct: 6,
      trailing_stop: { enabled: false, pct: 4 },
      rsi_above: { enabled: true, period: 14, threshold: 70 },
      macd_bearish_cross: { enabled: false, params: { fast: 12, slow: 26, signal: 9 } },
    },
    risk: { position_size_pct: 15, max_open_positions: 1, cooldown_bars: 5, max_loss_per_trade_pct: null as unknown as number },
    filters: {
      market_hours: { enabled: false, start_hour_utc: 13, end_hour_utc: 20, weekdays_only: true },
      min_volume: { enabled: false, multiplier: 1.1, min_volume: null as unknown as number },
    },
    activation: "AUTO_EXECUTE",
    version: 1,
  };
}

export const EXIT_LABEL: Record<ExitReason, string> = {
  TRAILING_STOP: "Trailing Stop", TAKE_PROFIT: "Take Profit", STOP_LOSS: "Stop Loss", RSI_EXIT: "RSI Exit",
  MACD_EXIT: "MACD Exit", END: "End", MAX_LOSS: "Max Loss", MANUAL: "Manual", EMERGENCY_STOP: "Emergency Stop",
};

/** Human summary like the simulator's summary box. */
export function describeEntry(s: FullStrategy): string[] {
  const e = s.entry, a: string[] = [];
  if (e.rsi_below.enabled) a.push(`RSI(${e.rsi_below.period})<${e.rsi_below.threshold}`);
  if (e.price_above_sma.enabled) a.push(`Price>SMA${e.price_above_sma.period}`);
  if (e.price_above_ema.enabled) a.push(`Price>EMA${e.price_above_ema.period}`);
  if (e.macd_bullish_cross.enabled) a.push("MACD cross");
  if (e.bollinger_lower.enabled) a.push("BB lower");
  if (e.volume_above_avg.enabled) a.push(`Volume>${e.volume_above_avg.multiplier}×avg`);
  if (e.breakout.enabled) a.push(`Breakout ${e.breakout.direction} (${e.breakout.lookback})`);
  return a;
}

interface Ind {
  close: number[]; rsiE: (number | null)[]; rsiX: (number | null)[]; smaV: (number | null)[]; emaV: (number | null)[];
  macdE: ReturnType<typeof macd>; macdX: ReturnType<typeof macd>; bb: ReturnType<typeof bollinger>;
  vAvg: (number | null)[]; fAvg: (number | null)[];
}

function indicators(s: FullStrategy, c: Candle[]): Ind {
  const close = c.map((x) => x.close), vol = c.map((x) => x.volume), e = s.entry;
  return {
    close,
    rsiE: rsi(close, e.rsi_below.period),
    rsiX: rsi(close, s.exit.rsi_above.period),
    smaV: sma(close, e.price_above_sma.period),
    emaV: ema(close, e.price_above_ema.period),
    macdE: macd(close, e.macd_bullish_cross.params.fast, e.macd_bullish_cross.params.slow),
    macdX: macd(close, s.exit.macd_bearish_cross.params.fast, s.exit.macd_bearish_cross.params.slow),
    bb: bollinger(close, e.bollinger_lower.period, e.bollinger_lower.std_dev),
    // backend uses a rolling 20-bar volume average (no look-ahead) when period is unset
    vAvg: volumeAvg(vol, e.volume_above_avg.period ?? 20),
    fAvg: volumeAvg(vol, e.volume_above_avg.period ?? 20),
  };
}

export interface EntryEval { ok: boolean; met: string[]; failed: string[]; blocked?: string }

function inMarketHours(s: FullStrategy, ts: string): boolean {
  const m = s.filters.market_hours;
  if (!m.enabled) return true;
  const d = new Date(ts), h = d.getUTCHours(), wd = d.getUTCDay();
  if (m.weekdays_only && (wd === 0 || wd === 6)) return false;
  return h >= m.start_hour_utc && h < m.end_hour_utc;
}

function evalEntry(s: FullStrategy, c: Candle[], ind: Ind, i: number): EntryEval {
  const e = s.entry, met: string[] = [], failed: string[] = [], px = c[i].close;
  const push = (name: string, v: boolean) => (v ? met : failed).push(name);
  // Condition ids match Bot 2's engine: EntryConditions field names + market_hours, min_volume, cooldown, max_open_positions
  if (!inMarketHours(s, c[i].timestamp)) return { ok: false, met, failed: ["market_hours"], blocked: "outside market hours" };
  if (s.filters.market_hours.enabled) met.push("market_hours");
  const mv = s.filters.min_volume;
  if (mv.enabled) {
    const avg = ind.fAvg[i];
    if ((avg != null && c[i].volume <= avg * mv.multiplier) || (mv.min_volume != null && c[i].volume < mv.min_volume))
      return { ok: false, met, failed: ["min_volume"], blocked: "volume below minimum (filter)" };
    met.push("min_volume");
  }
  if (e.rsi_below.enabled && ind.rsiE[i] != null) push("rsi_below", ind.rsiE[i]! < e.rsi_below.threshold);
  if (e.price_above_sma.enabled && ind.smaV[i] != null) push("price_above_sma", px > ind.smaV[i]!);
  if (e.price_above_ema.enabled && ind.emaV[i] != null) push("price_above_ema", px > ind.emaV[i]!);
  const { line, signal } = ind.macdE;
  if (e.macd_bullish_cross.enabled && line[i] != null && signal[i] != null && line[i - 1] != null && signal[i - 1] != null)
    push("macd_bullish_cross", line[i]! > signal[i]! && line[i - 1]! <= signal[i - 1]!);
  if (e.bollinger_lower.enabled && ind.bb.lower[i] != null)
    push("bollinger_lower", px < ind.bb.lower[i]! * (1 + e.bollinger_lower.proximity_pct / 100));
  if (e.volume_above_avg.enabled && ind.vAvg[i] != null) push("volume_above_avg", c[i].volume > ind.vAvg[i]! * e.volume_above_avg.multiplier);
  if (e.breakout.enabled && i >= e.breakout.lookback) {
    const w = c.slice(i - e.breakout.lookback, i);
    push("breakout", e.breakout.direction === "HIGH" ? px > Math.max(...w.map((x) => x.high)) : px < Math.min(...w.map((x) => x.low)));
  }
  const n = met.length + failed.length - (s.filters.market_hours.enabled ? 1 : 0) - (s.filters.min_volume.enabled ? 1 : 0);
  if (n === 0) return { ok: false, met, failed, blocked: "no active conditions" };
  return { ok: e.logic === "AND" ? failed.length === 0 : met.length > 0, met, failed };
}

/** Evaluate the entry rules on the last bar (mock live loop). */
export function evaluateLast(s: FullStrategy, c: Candle[]): EntryEval {
  return evalEntry(s, c, indicators(s, c), c.length - 1);
}

interface OpenPos { entry: number; size: number; qty: number; trail: number | null; t: string; bar: number }

export function backtest(s: FullStrategy, c: Candle[], initialCapital = DEFAULT_INITIAL_CAPITAL): BacktestResult & { buyBars: number[]; sellBars: number[] } {
  const ind = indicators(s, c), x = s.exit, r = s.risk;
  let capital = initialCapital, lastExit = -999, positions: OpenPos[] = [];
  const trades: BacktestTrade[] = [], equity: EquityPoint[] = [], buyBars: number[] = [], sellBars: number[] = [];
  const close = (p: OpenPos, i: number, reason: ExitReason, fill = c[i].close) => {
    const pct = (fill - p.entry) / p.entry, pnl = p.size * pct;
    capital += pnl;
    trades.push({ symbol: s.symbol, entry_time: p.t, entry_price: p.entry, exit_time: c[i].timestamp, exit_price: fill,
      quantity: p.qty, pnl, pnl_pct: pct * 100, exit_reason: reason });
    sellBars.push(i);
  };
  for (let i = SIM_WARMUP_BARS; i < c.length; i++) {
    const px = c[i].close;
    const bar = c[i];
    positions = positions.filter((p) => {
      // Backend exit semantics (intrabar): stops vs the bar's low (trailing uses the PREVIOUS bar's level),
      // targets vs the bar's high, stop-loss wins when both are hit; fills at the level, or at the open on a gap.
      let reason: ExitReason | null = null, fill = px;
      const slLevel = p.entry * (1 - x.stop_loss_pct / 100), tpLevel = p.entry * (1 + x.take_profit_pct / 100);
      const trailHit = x.trailing_stop.enabled && p.trail != null && bar.low <= p.trail;
      const slHit = bar.low <= slLevel;
      if (trailHit || slHit) {
        const useTrail = trailHit && (!slHit || (p.trail as number) >= slLevel);
        const level = useTrail ? (p.trail as number) : slLevel;
        reason = useTrail ? "TRAILING_STOP" : "STOP_LOSS";
        fill = Math.min(bar.open, level);
      } else if (bar.high >= tpLevel) {
        reason = "TAKE_PROFIT"; fill = Math.max(bar.open, tpLevel);
      }
      if (!reason && x.trailing_stop.enabled && p.trail != null) p.trail = Math.max(p.trail, bar.high * (1 - x.trailing_stop.pct / 100));
      const pct = ((px - p.entry) / p.entry) * 100;
      if (!reason && r.max_loss_per_trade_pct != null && p.size * pct / 100 <= -(initialCapital * r.max_loss_per_trade_pct) / 100) reason = "MAX_LOSS";
      if (!reason && x.rsi_above.enabled && ind.rsiX[i] != null && ind.rsiX[i]! > x.rsi_above.threshold) reason = "RSI_EXIT";
      const { line, signal } = ind.macdX;
      if (!reason && x.macd_bearish_cross.enabled && line[i] != null && signal[i] != null && line[i - 1] != null &&
        line[i]! < signal[i]! && line[i - 1]! > signal[i - 1]!) reason = "MACD_EXIT";
      if (reason) { close(p, i, reason, fill); lastExit = i; return false; }
      return true;
    });
    if (positions.length < r.max_open_positions && i - lastExit > r.cooldown_bars && evalEntry(s, c, ind, i).ok) {
      const size = capital * (r.position_size_pct / 100);
      positions.push({ entry: px, size, qty: size / px, trail: x.trailing_stop.enabled ? px * (1 - x.trailing_stop.pct / 100) : null, t: c[i].timestamp, bar: i });
      buyBars.push(i);
    }
    equity.push({ timestamp: c[i].timestamp, equity: capital });
  }
  positions.forEach((p) => close(p, c.length - 1, "END"));
  if (equity.length) equity[equity.length - 1] = { ...equity[equity.length - 1], equity: capital };
  let peak = initialCapital, mdd = 0;
  for (const e of equity) { peak = Math.max(peak, e.equity); mdd = Math.max(mdd, (peak - e.equity) / peak); }
  const wins = trades.filter((t) => (t.pnl ?? 0) > 0).length;
  return {
    strategy_name: s.name, symbol: s.symbol, initial_capital: initialCapital, final_equity: capital,
    total_return_pct: ((capital - initialCapital) / initialCapital) * 100, max_drawdown_pct: mdd * 100,
    win_rate_pct: trades.length ? (wins / trades.length) * 100 : 0, num_trades: trades.length, trades, equity_curve: equity,
    buyBars, sellBars,
  };
}

/** Deep-merge a (possibly partial) ManualStrategy onto the defaults. */
export function fillStrategy(s: ManualStrategy): FullStrategy {
  const merge = (a: any, b: any): any => {
    if (b === undefined) return a;
    if (a && typeof a === "object" && !Array.isArray(a) && b && typeof b === "object") {
      const o: any = { ...a };
      for (const k of Object.keys(b)) o[k] = merge(a[k], b[k]);
      return o;
    }
    return b;
  };
  return merge(defaultStrategy(), s);
}

/** Friendly labels for Signal.conditions_met / conditions_failed ids (Bot 2's engine). Unknown ids pass through. */
export const CONDITION_LABEL: Record<string, string> = {
  rsi_below: "RSI below threshold", price_above_sma: "Price above SMA", price_above_ema: "Price above EMA",
  macd_bullish_cross: "MACD bullish cross", bollinger_lower: "Near lower Bollinger", volume_above_avg: "Volume above average",
  breakout: "Breakout", market_hours: "Market hours", min_volume: "Min volume", cooldown: "Cooldown elapsed",
  max_open_positions: "Position slot free",
  // Auto-mode preset conditions emitted by Bot 2's engine (backend/engine/presets.py)
  close_above_ema: "Close above EMA", ema_above_sma: "EMA above SMA", macd_above_signal: "MACD above signal",
  dca_schedule: "DCA schedule bar", near_sma20: "Near SMA20", drop_from_high: "Drop from high", rsi_recovered: "RSI recovered",
  trend_broken: "Trend broken", trend_lost: "Trend lost", warmup: "Indicators warmed up", range: "Range wide enough",
  in_range: "Inside range", bottom_cell: "Bottom grid cell", top_cell: "Top grid cell",
  // LIVE safety (Bot 2 engine, bot2/live) and LIVE hard caps (Bot 3 risk, bot3/live)
  insufficient_data: "Not enough history yet (needs 61 bars)",
  live_stale_data: "Stale data", live_future_data: "Data timestamp in the future", live_no_data: "No market data",
  live_no_volume: "No volume on the last bar", live_thin_liquidity: "Thin liquidity, size reduced or skipped",
  live_market_hours: "Outside market hours", live_market_hours_now: "Outside market hours (now)",
  live_allowed: "LIVE allowed (allow_live)", live_daily_loss_cap: "Daily loss cap (auto e-stop)", live_gas_reserve: "BNB gas reserve",
  live_quote: "Valid quote", live_quote_notional: "Quote amount matches", live_slippage: "Slippage within limit",
  live_wallet_quote: "Wallet can cover quote", live_daily_notional: "Daily traded cap", live_max_exposure: "Max open exposure",
  live_order_cap: "Per-order cap", live_wallet_balance: "Wallet balance",
  live_below_min_order: "Below minimum order", order_in_flight: "Earlier swap still settling, waiting", pending_exit: "Exit queued until fill", exit_pending: "Exit queued until fill", session_expired: "Agentic Wallet session expired, re-scan QR on host PC", wallet_not_ready: "Agentic Wallet session expired, re-scan QR on host PC", live_illiquid_family: "xStock excluded (no liquidity on BNB Chain)", live_token_paused: "Token paused", live_price_impact: "Price impact above limit",
  // Binance Web3 exchange quote errors (Bot 4) used as market-state signals: a stay-out, not a failed trade
  // Bot 4 exchange reasons and Bot 3 wallet / live checks
  market_closed: "Market closed", insufficient_liquidity: "Insufficient liquidity", below_minimum: "Below minimum order", insufficient_gas: "Not enough BNB for gas, top up ~0.005 BNB",
  above_maximum: "Above maximum order", xstock_disabled: "xStock disabled (no liquidity on BSC)", slippage_exceeded: "Slippage exceeded",
  wallet_limits: "Wallet limits", wallet_allowed_token: "Token allowed by wallet", wallet_per_tx_cap: "Wallet per-trade cap",
  wallet_daily_limit: "Wallet daily limit", wallet_exit_reserve: "Exit reserve (quota to sell all positions)",
  live_token_status: "Token tradable now", live_symbol_allowed: "Token allowed", min_order_notional: "Minimum order size",
  "40366": "Above Ondo max single order", "40367": "Ondo market not tradable now (off-hours)", "40369": "bStock outside trading time",
  "40374": "Insufficient liquidity", "40375": "Below Ondo 20 USD minimum order",
};
/** Friendly label for a Signal.conditions_met/failed id (rule field, risk check, preset condition or ExitReason). */
export const conditionLabel = (id: string) =>
  CONDITION_LABEL[id] ?? (id in EXIT_LABEL ? EXIT_LABEL[id as ExitReason] : id.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase()));
/** ControlState.reason without the backend's "EMERGENCY STOP:" / "PAUSED:" prefix (the UI already says which). */
export const controlReason = (r?: string | null) => (r ?? "").replace(/^\s*(EMERGENCY[ _]STOP|PAUSED)\s*:\s*/i, "").trim();
/** Exchange error codes found in a reason text (e.g. "QuoteUnavailable 40367 ..."), as friendly labels. */
export const exchangeCodeLabels = (text?: string | null) =>
  [...new Set((text ?? "").match(/\b403(66|67|69|74|75)\b/g) ?? [])].map((c) => CONDITION_LABEL[c]);
/** Exit held because the position's buy is still settling on-chain (Bot 6 reason text or code). */
export const isExitQueued = (text?: string | null) => /still settling on-chain|settling on-chain|\b(pending_exit|exit_pending)\b|exit (held|queued)/i.test(text ?? "");
/** In-flight LIVE buy flagged by Bot 6 (Position.pending + client_order_id); tolerate missing fields. */
export const isPendingPos = (p: object) => (p as { pending?: boolean }).pending === true;
/** API v0.7: a held exit on a position ({code: 'pending_exit', reason, source}) or null. */
export const heldExit = (p: object) => (p as { held_exit?: { code?: string; reason?: string; source?: string } | null }).held_exit ?? null;
/** xStock (…x) on BNB Chain has no liquidity: greyed out in pickers, selectable only in SIM. */
export const isXStock = (sym: string) => /x$/.test(sym);
/** Signal.strategy / active_strategy "NONE" means Auto Mode is staying out. */
const STRATEGY_NAMES: Record<string, string> = { DCA: "DCA", SMART_DCA: "Smart DCA", BUY_THE_DIP: "Buy the Dip", TREND_FOLLOWING: "Trend / Momentum", TREND_MOMENTUM: "Trend / Momentum", GRID: "Grid" };
const REGIME_NAMES: Record<string, string> = { STRONG_UPTREND: "Strong uptrend", MILD_UPTREND: "Mild uptrend", SIDEWAYS: "Sideways", SIDEWAYS_RANGE: "Sideways", OVERSOLD_PULLBACK: "Oversold pullback", HIGH_VOLATILITY: "High volatility", DOWNTREND: "Downtrend" };
const STYLE_NAMES: Record<string, string> = { PATIENT: "Patient", BALANCED: "Balanced", ACTIVE: "Active" };
/** Human strategy name: preset label, "MANUAL:<name>" -> "<name> (manual)", "NONE" -> Staying out. */
export const strategyLabel = (s?: string | null) => {
  if (!s) return "—";
  if (s === "NONE") return "Staying out";
  if (s.startsWith("MANUAL:")) return `${s.slice(7)} (manual)`;
  return STRATEGY_NAMES[s] ?? s;
};
/** Replace raw engine names (regimes, presets, styles) inside a reason with labels; "->" becomes "picks". */
export const humanReason = (r?: string | null) => !r ? "" : r
  .replace(/\s*->\s*/g, " picks ")
  .replace(/\b[A-Z][A-Z_]{2,}\b/g, (w) => REGIME_NAMES[w] ?? STRATEGY_NAMES[w] ?? STYLE_NAMES[w] ?? w);
