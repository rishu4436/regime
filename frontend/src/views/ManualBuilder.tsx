import { useEffect, useRef, useState } from "react";
import { DEFAULT_INITIAL_CAPITAL, TimeframeValues, type AutoConfig, type ManualStrategy, type Timeframe } from "../contracts";
import { api } from "../api";
import { Card, NumberInput, RuleRow, Segmented, Toggle, useToast } from "../components/ui";
import { defaultStrategy, fillStrategy, TOKENS, type FullStrategy, isXStock } from "../lib/strategy";
import { BacktestResults, type BacktestRun } from "./BacktestResults";
import { ensureLoop } from "./Dashboard";
import { useLive } from "../lib/live";
import { TokenStatusBadge } from "../components/Live";

interface Props {
  strategy: FullStrategy; setStrategy: (s: FullStrategy) => void;
  capital: number; setCapital: (n: number) => void;
  run: BacktestRun | null; setRun: (r: BacktestRun) => void;
  onActivated: () => void;
  /** Called when the text parser returns an AutoConfig instead of a ManualStrategy. */
  onAutoParsed: (c: AutoConfig) => void;
}

/** Strip UI-only fields and send exactly the contract's ManualStrategy JSON. */

/** Bars per backtest; also the limit of the history candles request for its chart. */
const BACKTEST_BARS = 500;
export function toContract(s: FullStrategy, keepId = false): ManualStrategy {
  const { id, created_at, ...rest } = s;
  return structuredClone(keepId && id ? { ...rest, id, created_at } : rest);
}

export function ManualBuilder({ strategy: s, setStrategy, capital, setCapital, run, setRun, onActivated, onAutoParsed }: Props) {
  const { system } = useLive(); const simOnly = !system || system.trading_mode === "SIM"; // xStock only selectable in SIM
  const toast = useToast();
  const [busy, setBusy] = useState<"bt" | "act" | "nl" | "save" | null>(null);
  const [saved, setSaved] = useState<ManualStrategy[]>([]);
  const loadSaved = () => api.listStrategies().then(setSaved).catch(() => setSaved([]));
  useEffect(() => { loadSaved(); }, []);
  const save = async () => {
    setBusy("save");
    try {
      const out = s.id ? await api.updateStrategy(s.id, toContract(s, true)) : await api.saveStrategy(toContract(s));
      setStrategy({ ...fillStrategy(out), id: out.id, created_at: out.created_at });
      toast(`Saved "${out.name}"${out.version && out.version > 1 ? ` (v${out.version})` : ""}`, "success"); loadSaved();
    } catch (err) { toast(err instanceof Error ? err.message : String(err), "error"); } finally { setBusy(null); }
  };
  const remove = async () => {
    if (!s.id) return;
    try { await api.deleteStrategy(s.id); toast(`Deleted "${s.name}"`, "info"); setStrategy({ ...s, id: undefined, created_at: undefined }); loadSaved(); }
    catch (err) { toast(err instanceof Error ? err.message : String(err), "error"); }
  };
  const [text, setText] = useState("");
  const describe = async () => {
    if (!text.trim()) return;
    setBusy("nl");
    try {
      const out = await api.llmParse(text, s.symbol);
      if (out.kind === "MANUAL") { setStrategy({ ...fillStrategy(out.strategy), id: undefined }); toast("Form filled from your description. Review it, then run a backtest", "success"); }
      else { onAutoParsed(out.config); toast("That sounds like Auto Mode, so I opened it with your settings", "info"); }
    } catch (err) { toast(`Couldn't parse that: ${err instanceof Error ? err.message : err}`, "error"); }
    finally { setBusy(null); }
  };
  const fileRef = useRef<HTMLInputElement>(null);
  // immutable nested update: up(d => { d.entry.logic = "OR" })
  const up = (fn: (d: FullStrategy) => void) => { const d = structuredClone(s); fn(d); setStrategy(d); };
  const n = (v: number | null, fallback = 0) => (v == null || !isFinite(v) ? fallback : v);
  const e = s.entry, x = s.exit, r = s.risk, f = s.filters;
  const activeEntries = [e.rsi_below, e.price_above_sma, e.price_above_ema, e.macd_bullish_cross, e.bollinger_lower, e.volume_above_avg, e.breakout].filter((c) => c.enabled).length;

  const runBacktest = async () => {
    setBusy("bt");
    try {
      // same timeframe + limit for both, so /api/candles?history=true returns exactly the bars the backtest ran on
      const [result, candles] = await Promise.all([
        api.backtest({ strategy: toContract(s), initial_capital: capital, limit: BACKTEST_BARS }),
        api.backtestCandles(s.symbol, s.timeframe, BACKTEST_BARS),
      ]);
      setRun({ result, candles, strategy: structuredClone(s), ranAt: new Date().toISOString() });
      toast(`Backtest done: ${result.total_return_pct >= 0 ? "+" : ""}${result.total_return_pct.toFixed(2)}% over ${result.num_trades} trades`, result.total_return_pct >= 0 ? "success" : "warn");
    } catch (err) { toast(`Backtest failed: ${err instanceof Error ? err.message : err}`, "error"); }
    finally { setBusy(null); }
  };
  const activate = async () => {
    setBusy("act");
    try { await api.activateManual(s.id ? { strategy_id: s.id } : { strategy: toContract(s) }); await ensureLoop(); toast(`"${s.name}" is live on ${s.symbol} (${s.activation === "CONFIRMATION" ? "confirmation" : "auto-execute"})`, "success"); onActivated(); }
    catch (err) { toast(`Activation failed: ${err instanceof Error ? err.message : err}`, "error"); }
    finally { setBusy(null); }
  };
  const exportJson = () => {
    const blob = new Blob([JSON.stringify(toContract(s), null, 2)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = `${s.name.replace(/\W+/g, "_") || "strategy"}.json`; a.click();
    URL.revokeObjectURL(a.href);
  };
  const importJson = async (file: File) => {
    try { const v = await api.validateStrategy(JSON.parse(await file.text())); setStrategy({ ...fillStrategy(v), id: undefined }); toast(`Loaded ${file.name}`, "success"); }
    catch (err) { toast(err instanceof SyntaxError ? "That file is not JSON" : err instanceof Error ? err.message : String(err), "error"); }
  };

  return (
    <div className="builder">
      <div className="sticky">
        <Card pad={false} title="Strategy settings" extra={<>
          <button className="btn ghost" style={{ padding: "5px 10px" }} title="Import JSON" onClick={() => fileRef.current?.click()}>Import</button>
          <button className="btn ghost" style={{ padding: "5px 10px" }} title="Export JSON" onClick={exportJson}>Export</button>
          <input ref={fileRef} type="file" accept="application/json" hidden onChange={(ev) => { const fl = ev.target.files?.[0]; if (fl) importJson(fl); ev.target.value = ""; }} />
        </>}>
          <div className="group">
            <div className="ai-box">
              <span className="lbl">✨ Describe your strategy</span>
              <textarea value={text} placeholder="e.g. Buy TSLA when RSI is below 30 and price is above the 50 SMA, take profit 10%, stop loss 5%, trailing 3%, 20% per trade"
                onChange={(ev) => setText(ev.target.value)} onKeyDown={(ev) => { if (ev.key === "Enter" && (ev.metaKey || ev.ctrlKey)) describe(); }} />
              <div style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 8 }}>
                <span className="faint small" style={{ flex: 1 }}>{api.kind === "mock" ? "Mock keyword parser (Grok via backend in http mode)" : "Parsed by Grok on the backend"} · Ctrl+Enter</span>
                <button className="btn" style={{ padding: "6px 12px" }} disabled={busy != null || !text.trim()} onClick={describe}>{busy === "nl" ? <span className="spin" /> : null}Fill form</button>
              </div>
            </div>
          </div>
          <div className="group">
            <div className="field-grid">
              <div style={{ gridColumn: "1 / -1" }}>
                <span className="lbl">Saved strategies</span>
                <div style={{ display: "flex", gap: 8 }}>
                  <select value={s.id ?? ""} onChange={async (ev) => {
                    if (!ev.target.value) { setStrategy({ ...s, id: undefined, created_at: undefined }); return; }
                    try { const st = await api.getStrategy(ev.target.value); setStrategy({ ...fillStrategy(st), id: st.id, created_at: st.created_at }); }
                    catch (err) { toast(err instanceof Error ? err.message : String(err), "error"); }
                  }}>
                    <option value="">+ New (unsaved)</option>
                    {saved.map((x) => <option key={x.id} value={x.id}>{x.name} · {x.symbol}{x.version && x.version > 1 ? ` · v${x.version}` : ""}</option>)}
                  </select>
                  <button className="btn ghost" disabled={busy != null} onClick={save}>{busy === "save" ? <span className="spin" /> : s.id ? "Update" : "Save"}</button>
                  {s.id && <button className="btn ghost" title="Delete saved strategy" onClick={remove}>🗑</button>}
                </div>
              </div>
              <div style={{ gridColumn: "1 / -1" }}><span className="lbl">Strategy name</span>
                <input type="text" value={s.name} onChange={(ev) => up((d) => { d.name = ev.target.value; })} /></div>
              <div><span className="lbl">Tokenized stock<TokenStatusBadge symbol={s.symbol} /></span>
                <select value={s.symbol} onChange={(ev) => up((d) => { d.symbol = ev.target.value; })}>{TOKENS.map((t) => <option key={t.symbol} value={t.symbol} disabled={isXStock(t.symbol) && !simOnly} title={isXStock(t.symbol) ? "No liquidity on BSC" : undefined}>{t.label}{isXStock(t.symbol) ? " · No liquidity on BSC" : ""}</option>)}</select></div>
              <div><span className="lbl">Timeframe</span>
                <select value={s.timeframe} onChange={(ev) => up((d) => { d.timeframe = ev.target.value as Timeframe; })}>{TimeframeValues.map((t) => <option key={t}>{t}</option>)}</select></div>
            </div>
          </div>

          <div className="group">
            <div className="group-title">Entry conditions <span className="count pill">{activeEntries} active</span></div>
            <RuleRow on={e.rsi_below.enabled} onToggle={(v) => up((d) => { d.entry.rsi_below.enabled = v; })} title="RSI below" hint={`RSI(${e.rsi_below.period}) under threshold`}>
              <NumberInput value={e.rsi_below.period} min={2} width={62} onChange={(v) => up((d) => { d.entry.rsi_below.period = n(v, 14); })} />
              <NumberInput value={e.rsi_below.threshold} min={0} max={100} width={70} onChange={(v) => up((d) => { d.entry.rsi_below.threshold = n(v, 35); })} />
            </RuleRow>
            <RuleRow on={e.price_above_sma.enabled} onToggle={(v) => up((d) => { d.entry.price_above_sma.enabled = v; })} title="Price above SMA" hint="Close over the simple moving average">
              <NumberInput value={e.price_above_sma.period} min={2} onChange={(v) => up((d) => { d.entry.price_above_sma.period = n(v, 50); })} />
            </RuleRow>
            <RuleRow on={e.price_above_ema.enabled} onToggle={(v) => up((d) => { d.entry.price_above_ema.enabled = v; })} title="Price above EMA" hint="Close over the exponential moving average">
              <NumberInput value={e.price_above_ema.period} min={2} onChange={(v) => up((d) => { d.entry.price_above_ema.period = n(v, 20); })} />
            </RuleRow>
            <RuleRow on={e.macd_bullish_cross.enabled} onToggle={(v) => up((d) => { d.entry.macd_bullish_cross.enabled = v; })} title="MACD bullish crossover"
              hint={`${e.macd_bullish_cross.params.fast}/${e.macd_bullish_cross.params.slow}/${e.macd_bullish_cross.params.signal}: line crosses above signal`} />
            <RuleRow on={e.bollinger_lower.enabled} onToggle={(v) => up((d) => { d.entry.bollinger_lower.enabled = v; })} title="Near lower Bollinger Band"
              hint={`BB(${e.bollinger_lower.period}, ${e.bollinger_lower.std_dev}σ), within ${e.bollinger_lower.proximity_pct}%`}>
              <NumberInput value={e.bollinger_lower.proximity_pct} min={0} step={0.5} suffix="%" width={78} onChange={(v) => up((d) => { d.entry.bollinger_lower.proximity_pct = n(v, 2); })} />
            </RuleRow>
            <RuleRow on={e.volume_above_avg.enabled} onToggle={(v) => up((d) => { d.entry.volume_above_avg.enabled = v; })} title="Volume above average"
              hint={`volume > ${e.volume_above_avg.multiplier}× avg (${e.volume_above_avg.period ? `${e.volume_above_avg.period} bars` : "default window"})`}>
              <NumberInput value={e.volume_above_avg.multiplier} min={0} step={0.1} suffix="×" width={78} onChange={(v) => up((d) => { d.entry.volume_above_avg.multiplier = n(v, 1.1); })} />
            </RuleRow>
            <RuleRow on={e.breakout.enabled} onToggle={(v) => up((d) => { d.entry.breakout.enabled = v; })} title="Price-action breakout" hint="Break of the recent high/low (from the spec, not in the simulator)">
              <Segmented size="sm" value={e.breakout.direction} onChange={(v) => up((d) => { d.entry.breakout.direction = v; })} options={[{ value: "HIGH", label: "High" }, { value: "LOW", label: "Low" }]} />
              <NumberInput value={e.breakout.lookback} min={2} width={62} onChange={(v) => up((d) => { d.entry.breakout.lookback = n(v, 20); })} />
            </RuleRow>
            <div style={{ marginTop: 12 }}>
              <span className="lbl">Combine entry conditions with</span>
              <Segmented value={e.logic} onChange={(v) => up((d) => { d.entry.logic = v; })}
                options={[{ value: "AND", label: "AND · all must be true" }, { value: "OR", label: "OR · any is enough" }]} />
            </div>
          </div>

          <div className="group">
            <div className="group-title">Exit rules</div>
            <div className="field-grid" style={{ marginBottom: 6 }}>
              <div><span className="lbl">Take profit</span><NumberInput value={x.take_profit_pct} min={0} step={0.5} suffix="%" onChange={(v) => up((d) => { d.exit.take_profit_pct = n(v, 12); })} /></div>
              <div><span className="lbl">Stop loss</span><NumberInput value={x.stop_loss_pct} min={0} step={0.5} suffix="%" onChange={(v) => up((d) => { d.exit.stop_loss_pct = n(v, 6); })} /></div>
            </div>
            <RuleRow on={x.trailing_stop.enabled} onToggle={(v) => up((d) => { d.exit.trailing_stop.enabled = v; })} title="Trailing stop" hint="Ratchets up behind the price">
              <NumberInput value={x.trailing_stop.pct} min={0} step={0.5} suffix="%" width={78} onChange={(v) => up((d) => { d.exit.trailing_stop.pct = n(v, 4); })} />
            </RuleRow>
            <RuleRow on={x.rsi_above.enabled} onToggle={(v) => up((d) => { d.exit.rsi_above.enabled = v; })} title="Exit when RSI above" hint={`RSI(${x.rsi_above.period}) overbought`}>
              <NumberInput value={x.rsi_above.threshold} min={0} max={100} width={70} onChange={(v) => up((d) => { d.exit.rsi_above.threshold = n(v, 70); })} />
            </RuleRow>
            <RuleRow on={x.macd_bearish_cross.enabled} onToggle={(v) => up((d) => { d.exit.macd_bearish_cross.enabled = v; })} title="Exit on MACD bearish crossover" hint="Line crosses below signal" />
          </div>

          <div className="group">
            <div className="group-title">Position & risk</div>
            <div className="field-grid">
              <div><span className="lbl">Position size</span><NumberInput value={r.position_size_pct} min={1} max={100} suffix="%" onChange={(v) => up((d) => { d.risk.position_size_pct = Math.min(100, Math.max(1, n(v, 15))); })} /></div>
              <div><span className="lbl">Max open positions</span>
                <Segmented value={String(r.max_open_positions)} onChange={(v) => up((d) => { d.risk.max_open_positions = +v; })}
                  options={[{ value: "1", label: "1" }, { value: "2", label: "2" }, { value: "3", label: "3" }]} /></div>
              <div><span className="lbl">Cooldown after exit</span><NumberInput value={r.cooldown_bars} min={0} suffix="bars" onChange={(v) => up((d) => { d.risk.cooldown_bars = Math.max(0, n(v, 5)); })} /></div>
              <div><span className="lbl">Max loss per trade</span><NumberInput value={r.max_loss_per_trade_pct} nullable min={0} step={0.5} suffix="%" onChange={(v) => up((d) => { d.risk.max_loss_per_trade_pct = v as number; })} /></div>
            </div>
          </div>

          <div className="group">
            <div className="group-title">Filters</div>
            <RuleRow on={f.market_hours.enabled} onToggle={(v) => up((d) => { d.filters.market_hours.enabled = v; })} title="Only trade during US market hours"
              hint={`${f.market_hours.start_hour_utc}:00–${f.market_hours.end_hour_utc}:00 UTC${f.market_hours.weekdays_only ? ", weekdays" : ""}`} />
            <RuleRow on={f.min_volume.enabled} onToggle={(v) => up((d) => { d.filters.min_volume.enabled = v; })} title="Require above-average volume" hint="Hard filter, always ANDed with the entry rules">
              <NumberInput value={f.min_volume.multiplier} min={0} step={0.1} suffix="×" width={78} onChange={(v) => up((d) => { d.filters.min_volume.multiplier = n(v, 1.1); })} />
            </RuleRow>
          </div>

          <div className="group">
            <div className="field-grid">
              <div><span className="lbl">Starting capital</span><NumberInput value={capital} min={1} step={500} suffix="USDT" onChange={(v) => setCapital(n(v, DEFAULT_INITIAL_CAPITAL))} /></div>
              <div><span className="lbl">When live</span>
                <Segmented value={s.activation} onChange={(v) => up((d) => { d.activation = v; })} options={[{ value: "CONFIRMATION", label: "Confirm" }, { value: "AUTO_EXECUTE", label: "Auto" }]} /></div>
            </div>
            <button className="btn block lg" style={{ marginTop: 16 }} disabled={busy != null} onClick={runBacktest}>{busy === "bt" ? <span className="spin" /> : "▶"} Run backtest</button>
            <div className="field-grid" style={{ marginTop: 8 }}>
              <button className="btn green" disabled={busy != null} onClick={activate}>{busy === "act" ? <span className="spin" /> : "⚡"} Activate strategy</button>
              <button className="btn ghost" disabled={busy != null} onClick={() => { setStrategy(defaultStrategy()); setCapital(DEFAULT_INITIAL_CAPITAL); toast("Reset to simulator defaults", "info"); }}>Reset</button>
            </div>
            <div className="rule on" style={{ marginTop: 8 }}>
              <Toggle checked={api.kind === "mock"} disabled onChange={() => {}} label="Mock data" />
              <div className="rule-text"><div className="rule-hint">{api.kind === "mock" ? "Mock candles, backtest computed in the browser. Real runs use Binance Web3 market data via the backend." : "Backtest runs on the backend engine."}</div></div>
            </div>
          </div>
        </Card>
      </div>
      <BacktestResults run={run} loading={busy === "bt"} />
    </div>
  );
}
