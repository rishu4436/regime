import { useEffect, useState } from "react";
import { PresetNameValues, RegimeValues, TimeframeValues, TraderStyleValues, type AutoConfig, type RegimeAnalysis, type StatusSnapshot, type StrategySelection, type Timeframe } from "../contracts";
import { ensureLoop } from "./Dashboard";
import { api } from "../api";
import { RegimeBadge } from "../components/Badges";
import { Card, NumberInput, Segmented, Toggle, useToast } from "../components/ui";
import { PRESET_INFO, REGIME_INFO, STYLE_INFO, STYLE_MAP } from "../lib/auto";
import { strategyLabel, TOKENS } from "../lib/strategy";

interface Props { config: AutoConfig; setConfig: (c: AutoConfig) => void; status: StatusSnapshot | null; onActivated: () => void; tick: number }

export function AutoMode({ config: c, setConfig, status, onActivated, tick }: Props) {
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
  const pick = sel?.preset ?? (regime ? STYLE_MAP[regime][style][0] : null);
  const mult = sel?.size_multiplier ?? (regime ? STYLE_MAP[regime][style][1] : 1);
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
              <div><span className="lbl">Tokenized stock</span>
                <select value={c.symbol} onChange={(e) => setConfig({ ...c, symbol: e.target.value })}>{TOKENS.map((t) => <option key={t.symbol} value={t.symbol}>{t.label}</option>)}</select></div>
              <div><span className="lbl">Timeframe</span>
                <select value={c.timeframe ?? "1h"} onChange={(e) => setConfig({ ...c, timeframe: e.target.value as Timeframe })}>{TimeframeValues.map((t) => <option key={t}>{t}</option>)}</select></div>
              <div><span className="lbl">Capital allocated</span><NumberInput value={c.capital} min={1} step={500} suffix="USDT" onChange={(v) => setConfig({ ...c, capital: v ?? 1 })} /></div>
              <div><span className="lbl">Execution</span>
                <Segmented value={c.activation ?? "CONFIRMATION"} onChange={(v) => setConfig({ ...c, activation: v })} options={[{ value: "CONFIRMATION", label: "Confirm" }, { value: "AUTO_EXECUTE", label: "Auto" }]} /></div>
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
        <Card title="Regime × style → preset" extra={<span className="faint small">indicative; the engine (Bot 2) decides</span>} pad={false}>
          <div className="table-wrap" style={{ padding: "4px 8px 8px", maxHeight: "none" }}>
            <table className="map-table">
              <thead><tr><th>Regime</th>{TraderStyleValues.map((s) => <th key={s} style={s === style ? { color: "#c7d2fe" } : undefined}>{STYLE_INFO[s].label.split(" / ")[0]}</th>)}</tr></thead>
              <tbody>
                {RegimeValues.map((r) => (
                  <tr key={r}>
                    <td><RegimeBadge regime={r} /></td>
                    {TraderStyleValues.map((s) => {
                      const [p, m] = STYLE_MAP[r][s], hl = r === regime && s === style;
                      return <td key={s} className={hl ? "hl" : ""} style={{ opacity: p && !enabled.includes(p) ? 0.4 : 1 }}>
                        {p ? PRESET_INFO[p].label : <span className="faint">stay out</span>}{p && m !== 1 ? <span className="faint small"> ×{m}</span> : null}
                      </td>;
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
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
