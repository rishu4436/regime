import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";

/** iOS-style toggle switch. */
export function Toggle({ checked, onChange, disabled, label }: { checked: boolean; onChange: (v: boolean) => void; disabled?: boolean; label?: string }) {
  return (
    <button type="button" role="switch" aria-checked={checked} aria-label={label} disabled={disabled}
      className={`toggle${checked ? " on" : ""}`} onClick={() => onChange(!checked)}><span /></button>
  );
}

/** Rounded segmented control. */
export function Segmented<T extends string>({ value, options, onChange, size }: {
  value: T; options: readonly { value: T; label: ReactNode }[]; onChange: (v: T) => void; size?: "sm";
}) {
  return (
    <div className={`segmented${size === "sm" ? " sm" : ""}`} role="radiogroup">
      {options.map((o) => (
        <button type="button" key={o.value} role="radio" aria-checked={o.value === value}
          className={o.value === value ? "active" : ""} onClick={() => onChange(o.value)}>{o.label}</button>
      ))}
    </div>
  );
}

/** A labelled condition/rule row: toggle + text + optional inline inputs. */
export function RuleRow({ on, onToggle, title, hint, children }: { on: boolean; onToggle: (v: boolean) => void; title: ReactNode; hint?: ReactNode; children?: ReactNode }) {
  return (
    <div className={`rule${on ? " on" : ""}`}>
      <Toggle checked={on} onChange={onToggle} label={typeof title === "string" ? title : undefined} />
      <div className="rule-text"><div className="rule-title">{title}</div>{hint && <div className="rule-hint">{hint}</div>}</div>
      {children && <div className="rule-inputs">{children}</div>}
    </div>
  );
}

export function NumberInput({ value, onChange, min, max, step, suffix, width, disabled, nullable }: {
  value: number | null | undefined; onChange: (v: number | null) => void; min?: number; max?: number; step?: number; suffix?: string; width?: number; disabled?: boolean; nullable?: boolean;
}) {
  return (
    <div className="num" style={width ? { width } : undefined}>
      <input type="number" value={value ?? ""} min={min} max={max} step={step ?? 1} disabled={disabled}
        placeholder={nullable ? "—" : undefined}
        onChange={(e) => onChange(e.target.value === "" ? (nullable ? null : min ?? 0) : +e.target.value)} />
      {suffix && <span>{suffix}</span>}
    </div>
  );
}

/** Animated number (ticker style). */
export function Ticker({ value, format }: { value: number | null | undefined; format: (v: number) => string }) {
  const [shown, setShown] = useState(value ?? 0);
  const from = useRef(value ?? 0);
  const [flash, setFlash] = useState<"" | "up" | "down">("");
  useEffect(() => {
    if (value == null) return;
    const start = performance.now(), a = from.current, b = value;
    if (a !== b) setFlash(b > a ? "up" : "down");
    let raf = 0;
    const step = (t: number) => {
      const k = Math.min(1, (t - start) / 450), e = 1 - (1 - k) ** 3;
      setShown(a + (b - a) * e);
      if (k < 1) raf = requestAnimationFrame(step); else from.current = b;
    };
    raf = requestAnimationFrame(step);
    const f = window.setTimeout(() => setFlash(""), 700);
    return () => { cancelAnimationFrame(raf); window.clearTimeout(f); from.current = b; };
  }, [value]);
  if (value == null) return <span className="skeleton" style={{ width: 90, height: 22 }} />;
  return <span className={`ticker ${flash}`}>{format(shown)}</span>;
}

export function StatCard({ label, value, format, delta, deltaLabel, tone, icon }: {
  label: string; value: number | null | undefined; format: (v: number) => string; delta?: number | null; deltaLabel?: string; tone?: "pos" | "neg" | "auto"; icon?: ReactNode;
}) {
  const t = tone === "auto" ? (value == null || value === 0 ? "" : value > 0 ? "pos" : "neg") : tone ?? "";
  return (
    <div className="stat-card">
      <div className="stat-top"><span className="stat-label">{label}</span>{icon && <span className="stat-icon">{icon}</span>}</div>
      <div className={`stat-value ${t}`}><Ticker value={value} format={format} /></div>
      {delta != null && isFinite(delta) && (
        <div className={`delta ${delta > 0 ? "pos" : delta < 0 ? "neg" : ""}`}>{delta > 0 ? "▲" : delta < 0 ? "▼" : "•"} {Math.abs(delta).toFixed(2)}%{deltaLabel ? <span> {deltaLabel}</span> : null}</div>
      )}
    </div>
  );
}

export const Skeleton = ({ h = 16, w = "100%", style }: { h?: number; w?: number | string; style?: React.CSSProperties }) =>
  <div className="skeleton" style={{ height: h, width: w, ...style }} />;

export function Card({ title, extra, children, className, pad = true }: { title?: ReactNode; extra?: ReactNode; children: ReactNode; className?: string; pad?: boolean }) {
  return (
    <section className={`card${className ? " " + className : ""}`}>
      {(title || extra) && <header className="card-head"><h2>{title}</h2><div className="card-extra">{extra}</div></header>}
      <div className={pad ? "card-body" : ""}>{children}</div>
    </section>
  );
}

// ---------------------------------------------------------------- toasts
type ToastKind = "success" | "error" | "info" | "warn";
interface ToastItem { id: number; kind: ToastKind; text: string }
const ToastCtx = createContext<(text: string, kind?: ToastKind) => void>(() => {});
export const useToast = () => useContext(ToastCtx);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const push = useCallback((text: string, kind: ToastKind = "info") => {
    const id = Date.now() + Math.random();
    setItems((x) => [...x.slice(-3), { id, kind, text }]);
    window.setTimeout(() => setItems((x) => x.filter((t) => t.id !== id)), 4200);
  }, []);
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="toasts" aria-live="polite">
        {items.map((t) => <div key={t.id} className={`toast ${t.kind}`} onClick={() => setItems((x) => x.filter((y) => y.id !== t.id))}>{t.text}</div>)}
      </div>
    </ToastCtx.Provider>
  );
}
