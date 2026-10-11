import { useEffect, useState } from "react";
import { PresetNameValues, RegimeValues, TimeframeValues, TraderStyleValues, type AutoConfig, type RegimeAnalysis, type StatusSnapshot, type StrategySelection, type Timeframe } from "../contracts";
import { ensureLoop } from "./Dashboard";
import { api } from "../api";
import { RegimeBadge } from "../components/Badges";
import { Card, NumberInput, Segmented, Skeleton, Toggle, useToast } from "../components/ui";
import { PRESET_INFO, REGIME_INFO, STYLE_INFO } from "../lib/auto";
import type { PresetsTable } from "../api/types";
import { strategyLabel, TOKENS, isXStock } from "../lib/strategy";
import { useLive } from "../lib/live";
import { TokenStatusBadge } from "../components/Live";

interface Props { config: AutoConfig; setConfig: (c: AutoConfig) => void; status: StatusSnapshot | null; onActivated: () => void; tick: number }

export function AutoMode({ config: c, setConfig, status, onActivated, tick }: Props) {
  const { system } = useLive(); const simOnly = !system || system.trading_mode === "SIM"; // xStock only selectable in SIM
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const style = c.style ?? "BALANCED", enabled = c.enabled_presets ?? [...PresetNameValues];
  // live preview from the backend: GET /api/regime/{symbol} + GET /api/auto/selection
  const [ra, setRa] = useState<RegimeAnalysis | null>(null);
  const [sel, setSel] = useState<StrategySelection | null>(null);
  useEffect(() => {
    api.getRegime(c.symbol, c.timeframe ?? "1h").then(setRa).catch(() => setRa(null));
    api.getSelection(c.symbol, style, c.timeframe ?? "1h").then(setSel).catch(() => setSel(null));
  }, [c.symbol, c.timeframe, style, tick]);
  const regime = ra?.regime ?? null;
  // the engine's full map(regime, style) table: GET /api/auto/presets-table
  const [table, setTable] = useState<PresetsTable | null>(null);
  const [tableErr, setTableErr] = useState<string | null>(null);
  useEffect(() => { api.getPresetsTable().then((t) => { setTable(t); setTableErr(null); }).catch((e) => setTableErr(e instanceof Error ? e.message : String(e))); }, []);
  const cell = (r: string, st: string) => table?.entries.find((e) => e.regime === r && e.style === st);
  const fromTable = regime ? cell(regime, style) : undefined;
  const pick = sel ? sel.preset ?? null : fromTable?.preset ?? null;
  const mult = sel?.size_multiplier ?? fromTable?.size_multiplier ?? 1;
  const pickOn = pick != null && enabled.includes(pick);
  const isActive = status?.agent_mode === "AUTO";

  const togglePreset = (p: (typeof PresetNameValues)[number], on: boolean) => {
    const next = on ? [...enabled, p] : enabled.filter((x) => x !== p);
    if (!next.length) { toast("Keep at least one preset enabled", "warn"); return; }
    setConfig({ ...c, enabled_presets: PresetNameValues.filter((x) => next.includes(x)) });
  };
  const activate = async () => {
    setBusy(true);
    try { await api.activateAuto({ ...c, enabled_presets: enabled }); await ensureLoop(); toast(`Auto mode live: ${STYLE_INFO[style].label} on ${c.symbol}`, "success"); onActivated(); }
    catch (e) { toast(`Activation failed: ${e instanceof Error ? e.message : e}`, "error"); } finally { setBusy(false); }
  };

  return (
    <div className="builder">
      <div className="sticky">
        <Card title="Auto agent" extra={isActive && <span className="pill ok"><span className="live-dot" />Active</span>}>
          <div className="stack" style={{ gap: 14 }}>
            <div className="field-grid">
              <div><span className="lbl">Tokenized stock<TokenStatusBadge symbol={c.symbol} /></span>
                <select value={c.symbol} onChange={(e) => setConfig({ ...c, symbol: e.target.value })}>{TOKENS.map((t) => <option key={t.symbol} value={t.symbol} disabled={isXStock(t.symbol) && !simOnly} title={isXStock(t.symbol) ? "No liquidity on BSC" : undefined}>{t.label}{isXStock(t.symbol) ? " · No liquidity on BSC" : ""}</option>)}</select></div>
              <div><span className="lbl">Timeframe</span>
                <select value={c.timeframe ?? "1h"} onChange={(e) => setConfig({ ...c, timeframe: e.target.value as Timeframe })}>{TimeframeValues.map((t) => <option key={t}>{t}</option>)}</select></div>
              <div><span className="lbl">Capital allocated</span><NumberInput value={c.capital} min={1} step={500} suffix="USDT" onChange={(v) => setConfig({ ...c, capital: v ?? 1 })} /></div>
              <div><span className="lbl">Execution</span>
                <Segmented value={c.activation ?? "AUTO_EXECUTE"} onChange={(v) => setConfig({ ...c, activation: v })} options={[{ value: "AUTO_EXECUTE", label: "Autonomous" }, { value: "CONFIRMATION", label: "Ask me first" }]} /></div>
            </div>
            <div>
              <span className="lbl">Trader style</span>
              <div className="choice-grid" style={{ gridTemplateColumns: "1fr" }}>
                {TraderStyleValues.map((s) => (
                  <button key={s} className={`choice${s === style ? " active" : ""}`} onClick={() => setConfig({ ...c, style: s })}>
                    <b>{STYLE_INFO[s].label}</b><span>{STYLE_INFO[s].blurb}</span>
                  </button>
                ))}
              </div>
            </div>
            <div>
              <span className="lbl">Preset agents the selector may use</span>
              {PresetNameValues.map((p) => (
                <div key={p} className={`rule${enabled.includes(p) ? " on" : ""}`}>
                  <Toggle checked={enabled.includes(p)} onChange={(v) => togglePreset(p, v)} label={PRESET_INFO[p].label} />
                  <div className="rule-text"><div className="rule-title">{PRESET_INFO[p].label}</div><div className="rule-hint">{PRESET_INFO[p].blurb}</div></div>
                </div>
              ))}
            </div>
            <button className="btn green block lg" disabled={busy} onClick={activate}>{busy ? <span className="spin" /> : "⚡"} {isActive ? "Update auto agent" : "Start auto agent"}</button>
            <div className="note" style={{ marginTop: 0 }}>Flow: risk check → detect regime → map (regime, style) to exactly one preset → evaluate it → risk check → buy, ask you, or stay out.</div>
          </div>
        </Card>
      </div>

      <div className="stack">
        <Card title={<>Right now <span className="muted small" style={{ fontWeight: 400 }}>{c.symbol}</span></>} extra={<RegimeBadge regime={regime} />}>
          <div style={{ display: "flex", gap: 28, flexWrap: "wrap", alignItems: "center" }}>
            <div><div className="lbl">Regime</div><div className="stat-value" style={{ marginTop: 0, color: regime ? REGIME_INFO[regime].color : undefined }}>{regime ? REGIME_INFO[regime].label : "—"}</div></div>
            <div style={{ fontSize: "1.4rem", color: "var(--faint)" }}>→</div>
            <div><div className="lbl">With {STYLE_INFO[style].label}</div><div className="stat-value" style={{ marginTop: 0 }}>{pick ? (pickOn ? PRESET_INFO[pick].label : `${PRESET_INFO[pick].label} (disabled)`) : "Staying out"}</div></div>
            {pick && mult !== 1 && <span className="pill warn">size × {mult}</span>}
          </div>
          {ra && <div className="small muted" style={{ marginTop: 12 }}>{ra.reason} · confidence {Math.round(ra.confidence * 100)}%</div>}
          {sel && <div className="small muted" style={{ marginTop: 4 }}>{sel.reason}</div>}
          {isActive && <div className="summary-box" style={{ marginTop: 14 }}>
            <b>Live:</b> {strategyLabel(status?.active_strategy)} · {status?.last_decision?.reason ?? "waiting for the next bar"}
          </div>}
        </Card>
        <Card title="Regime × style → preset"
          extra={table && <span className={`pill ${table.source.startsWith("engine:") ? "ok" : "warn"}`} title="Where the table comes from">{table.source}</span>} pad={false}>
          <div className="table-wrap" style={{ padding: "4px 8px 8px", maxHeight: "none" }}>
            {tableErr ? <div className="empty">Couldn't load the presets table: {tableErr}</div> : !table ? <Skeleton h={260} /> : (
              <table className="map-table">
                <thead><tr><th>Regime</th>{TraderStyleValues.map((s) => <th key={s} style={s === style ? { color: "#c7d2fe" } : undefined}>{STYLE_INFO[s].label.split(" / ")[0]}</th>)}</tr></thead>
                <tbody>
                  {RegimeValues.map((r) => (
                    <tr key={r}>
                      <td><RegimeBadge regime={r} /></td>
                      {TraderStyleValues.map((s) => {
                        const e = cell(r, s), hl = r === regime && s === style, p = e?.preset ?? null;
                        return (
                          <td key={s} className={`wrap${hl ? " hl" : ""}`} style={{ minWidth: 170, opacity: p && !enabled.includes(p) ? 0.45 : 1, verticalAlign: "top" }}>
                            <div>{!e ? <span className="faint">—</span> : p ? <b>{PRESET_INFO[p].label}</b> : <span className="neg">Stay out</span>}
                              {e && p && <span className={`small ${e.size_multiplier !== 1 ? "pos" : "faint"}`}> ×{e.size_multiplier}</span>}</div>
                            {e?.reason && <div className="faint small" style={{ marginTop: 3, whiteSpace: "normal" }}>{e.reason}</div>}
                          </td>
                        );
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            {table && <div className="note" style={{ padding: "0 4px" }}>All presets enabled. Cells dimmed when you've disabled that preset; the agent then stays out instead.</div>}
          </div>
        </Card>
        <Card title="Preset agents">
          <div className="choice-grid">
            {PresetNameValues.map((p) => (
              <div key={p} className={`choice${p === pick && pickOn ? " active" : ""}${enabled.includes(p) ? "" : " off"}`} style={{ cursor: "default" }}>
                <b>{PRESET_INFO[p].label}</b><span>{PRESET_INFO[p].blurb}</span>
              </div>
            ))}
          </div>
        </Card>
      </div>
    </div>
  );
}
