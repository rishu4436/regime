// Mock of Bot 5's parse_strategy_from_text: a tiny keyword/regex parser so the "Describe your strategy" box
// works offline. The real one (Grok + validation) is reached via POST /api/llm/parse in http mode.
import type { AutoConfig, ManualStrategy, TraderStyle } from "../contracts";
import { defaultStrategy } from "./strategy";

const numAfter = (t: string, re: RegExp) => { const m = t.match(re); const g = m?.slice(1).find((x) => x != null); return g != null ? parseFloat(g.replace(/,/g, "")) : undefined; };

export function parseStrategyText(text: string, defaultSymbol = "NVDAB"): ManualStrategy | AutoConfig {
  const t = text.toLowerCase();
  const symbol = /tsla|tesla/.test(t) ? "TSLAx" : /aapl|apple/.test(t) ? "AAPLB" : /ondo|nvdaon/.test(t) ? "NVDAon" : /nvda|nvidia/.test(t) ? "NVDAB" : defaultSymbol;
  const capital = numAfter(t, /\$\s?(\d[\d,]*)|(\d[\d,]*)\s*(?:usdt|usd|dollars)/);

  // Auto Mode: "auto", "let the agent decide", a style word with no indicator rules
  const style: TraderStyle | undefined = /patient|accumulat|long[- ]term/.test(t) ? "PATIENT" : /active|scalp|aggressive/.test(t) ? "ACTIVE" : /balanced|swing/.test(t) ? "BALANCED" : undefined;
  const hasRules = /rsi|sma|ema|macd|bollinger|\bbb\b|volume|breakout|take profit|stop loss|trailing/.test(t);
  if (/\bauto\b(?![- ]?execut)|auto mode|agent decide|you decide|pick for me/.test(t) || (style && !hasRules)) {
    return { symbol, style: style ?? "BALANCED", capital: capital ?? 10000, activation: /\b(confirm|ask me|approve|approval)\b/.test(t) && !/without (asking|confirm)/.test(t) ? "CONFIRMATION" : "AUTO_EXECUTE" };
  }

  const s = defaultStrategy();
  s.symbol = symbol;
  s.name = text.trim().slice(0, 48) || "Parsed strategy";
  // entry: start from nothing, enable what is mentioned
  for (const k of ["rsi_below", "price_above_sma", "price_above_ema", "macd_bullish_cross", "bollinger_lower", "volume_above_avg", "breakout"] as const) s.entry[k].enabled = false;
  const rsiBelow = numAfter(t, /rsi[^.,;]*?(?:below|under|<)\s*(\d+)/);
  if (rsiBelow != null || /oversold/.test(t)) { s.entry.rsi_below.enabled = true; s.entry.rsi_below.threshold = rsiBelow ?? 30; }
  const sma = numAfter(t, /(?:above|over)[^.,;]*?(\d+)[- ]?(?:period |day |bar )?sma|sma\s*\(?(\d+)/);
  if (/sma|moving average/.test(t)) { s.entry.price_above_sma.enabled = true; if (sma) s.entry.price_above_sma.period = sma; }
  const ema = numAfter(t, /(\d+)[- ]?(?:period |day |bar )?ema|ema\s*\(?(\d+)/);
  if (/\bema\b|ema\d|\dema/.test(t)) { s.entry.price_above_ema.enabled = true; if (ema) s.entry.price_above_ema.period = ema; }
  if (/macd/.test(t) && /(bull|cross|buy)/.test(t)) s.entry.macd_bullish_cross.enabled = true;
  if (/bollinger|\bbb\b|lower band/.test(t)) s.entry.bollinger_lower.enabled = true;
  if (/volume/.test(t)) s.entry.volume_above_avg.enabled = true;
  if (/breakout|break (of|above) (the )?(recent )?high/.test(t)) s.entry.breakout.enabled = true;
  if (/\bor\b|any of|either/.test(t)) s.entry.logic = "OR";
  if (!Object.values(s.entry).some((c) => typeof c === "object" && c.enabled)) { s.entry.rsi_below.enabled = true; s.entry.price_above_sma.enabled = true; }
  // exits
  const tp = numAfter(t, /(?:take profit|tp|profit)[^\d]{0,12}(\d+(?:\.\d+)?)\s*%/) ?? numAfter(t, /(\d+(?:\.\d+)?)\s*%\s*(?:take profit|tp|profit|target)/);
  const sl = numAfter(t, /(?:stop loss|stop-loss|sl|stop)[^\d]{0,12}(\d+(?:\.\d+)?)\s*%/) ?? numAfter(t, /(\d+(?:\.\d+)?)\s*%\s*(?:stop loss|stop|sl)/);
  const trail = numAfter(t, /trailing[^\d]{0,16}(\d+(?:\.\d+)?)\s*%/);
  if (tp != null) s.exit.take_profit_pct = tp;
  if (sl != null) s.exit.stop_loss_pct = sl;
  if (trail != null) { s.exit.trailing_stop.enabled = true; s.exit.trailing_stop.pct = trail; }
  const rsiExit = numAfter(t, /(?:exit|sell)[^.,;]*?rsi[^.,;]*?(?:above|over|>)\s*(\d+)/);
  if (rsiExit != null) { s.exit.rsi_above.enabled = true; s.exit.rsi_above.threshold = rsiExit; }
  if (/macd/.test(t) && /bear|exit on macd|sell on macd/.test(t)) s.exit.macd_bearish_cross.enabled = true;
  // risk / filters
  const size = numAfter(t, /(\d+(?:\.\d+)?)\s*%\s*(?:of (?:my )?(?:capital|portfolio)|position|size|per trade)/);
  if (size != null) s.risk.position_size_pct = Math.min(100, size);
  const maxPos = numAfter(t, /(?:max(?:imum)?|up to)\s*(\d)\s*(?:open )?positions?/);
  if (maxPos != null) s.risk.max_open_positions = Math.min(3, Math.max(1, maxPos));
  const cool = numAfter(t, /cooldown[^\d]{0,10}(\d+)/) ?? numAfter(t, /wait\s*(\d+)\s*bars/);
  if (cool != null) s.risk.cooldown_bars = cool;
  if (/market hours|us session|trading hours/.test(t)) s.filters.market_hours.enabled = true;
  if (/(above[- ]average|high|min(?:imum)?) volume (filter|only)|only .*high volume/.test(t)) s.filters.min_volume.enabled = true;
  if (/\b(confirm|ask me|approve|approval)\b/.test(t) && !/without (asking|confirm)/.test(t)) s.activation = "CONFIRMATION";
  if (/auto[- ]?execute|without (asking|confirmation)/.test(t)) s.activation = "AUTO_EXECUTE";
  return s;
}
