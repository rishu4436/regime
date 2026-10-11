// "Register on-chain": BNB Agent Studio ERC-8004 identity from the dashboard (Bot 5's /api/agent-studio).
// 1. create an identity-only wallet on the PC (key stays in a git-ignored file, never sent to the browser)
// 2. fund it with ~0.002 BNB for gas  3. register on mainnet. The trading wallet (Agentic Wallet) is never touched.
import { useCallback, useEffect, useState } from "react";
import qrcode from "qrcode-generator";
import { api } from "../api";
import { ApiError, type AgentStudioStatus } from "../api/types";
import { bscAddr, shortHex } from "../lib/live";
import { useToast } from "./ui";

const errText = (e: unknown) => e instanceof ApiError && e.status === 403
  ? "This only works on the PC running the Regime backend. Open the dashboard there (http://localhost) and try again."
  : e instanceof ApiError && e.status === 504
  ? `The registration was sent but isn't confirmed yet. Check it on BscScan before trying again. ${e.message}`
  : e instanceof ApiError && e.status === 503
  ? `Agent Studio isn't installed on this PC yet (${e.message}). Run: pip install -r backend/agent_studio/requirements.txt`
  : e instanceof Error ? e.message : String(e);

function AddrQr({ addr }: { addr: string }) {
  const [src] = useState(() => { try { const q = qrcode(0, "M"); q.addData(addr); q.make(); return q.createDataURL(4, 8); } catch { return null; } });
  return src ? <img src={src} width={132} height={132} alt="Identity wallet address QR code" style={{ background: "#fff", borderRadius: 10, padding: 6, imageRendering: "pixelated" }} /> : null;
}

/** Shown inside the Agent identity card while the agent isn't registered. Hidden if the backend has no such flow. */
export function RegisterOnChain({ onRegistered }: { onRegistered?: () => void }) {
  const toast = useToast();
  const [st, setSt] = useState<AgentStudioStatus | null | undefined>(undefined);
  const [busy, setBusy] = useState<"key" | "reg" | null>(null);
  const [confirm, setConfirm] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const load = useCallback(() => api.getAgentStudio().then(setSt).catch(() => setSt(null)), []);
  useEffect(() => { load(); }, [load]);
  // while waiting for the BNB to arrive, re-check the balance every 10 s
  const needsFunds = !!st?.has_key && !st.registered && (st.bnb_balance ?? 0) < (st.bnb_needed ?? 0.002);
  useEffect(() => { if (!needsFunds) return; const id = setInterval(load, 10_000); return () => clearInterval(id); }, [needsFunds, load]);

  if (st === undefined || st === null || st.registered) return null;
  const netLabel = /^(bsc[ _-]?)?mainnet$/i.test(st.network ?? "mainnet") ? "BNB Chain mainnet" : /testnet/i.test(st.network ?? "") ? "BNB Chain testnet" : st.network!;
  const need = st.bnb_needed ?? 0.002, bal = st.bnb_balance ?? 0, step = !st.has_key ? 1 : needsFunds ? 2 : 3;

  const createKey = async () => {
    setBusy("key"); setErr(null);
    try { await api.createAgentStudioKey(); await load(); toast("Identity wallet created on this PC. Back up its key from the Regime folder", "success"); }
    catch (e) { setErr(errText(e)); } finally { setBusy(null); }
  };
  const register = async () => {
    setBusy("reg"); setErr(null);
    try { const r = await api.registerAgentStudio(st.network ?? "mainnet"); toast(`Registered on-chain: ERC-8004 id ${r.erc8004_id}`, "success"); await load(); onRegistered?.(); }
    catch (e) { setErr(errText(e)); } finally { setBusy(null); }
  };
  const Step = ({ n, title }: { n: number; title: string }) => (
    <div className="small" style={{ fontWeight: 600, marginTop: n > 1 ? 10 : 0, opacity: step >= n ? 1 : 0.45 }}>
      {step > n ? "✓" : `${n}.`} {title}</div>);

  return (
    <div className="group" style={{ marginTop: 12, paddingTop: 12, borderTop: "1px solid var(--border)" }}>
      <div className="small" style={{ marginBottom: 8 }}><b>Register on-chain</b> <span className="faint">· gives Regime a public ERC-8004 identity on BNB Chain. It uses a separate small wallet and never touches trading funds.</span></div>

      <Step n={1} title="Create identity wallet" />
      {step === 1 && <div style={{ marginTop: 6 }}><button className="btn" disabled={busy != null} onClick={createKey}>{busy === "key" ? <span className="spin" /> : "Create identity wallet"}</button>
        <div className="small faint" style={{ marginTop: 4 }}>The key is made and kept on this PC only.</div></div>}

      <Step n={2} title={`Fund it with about ${need} BNB for gas`} />
      {step === 2 && st.identity_address && <div style={{ display: "flex", gap: 12, alignItems: "center", marginTop: 6, flexWrap: "wrap" }}>
        <AddrQr addr={st.identity_address} />
        <div className="small" style={{ minWidth: 0 }}>
          <div className="mono" style={{ wordBreak: "break-all" }}>{st.identity_address}</div>
          <div style={{ marginTop: 6, display: "flex", gap: 6 }}>
            <button className="btn ghost" style={{ padding: "2px 10px" }} onClick={() => navigator.clipboard?.writeText(st.identity_address!).then(() => toast("Address copied", "info"))}>Copy</button>
            <a className="btn ghost" style={{ padding: "2px 10px" }} href={bscAddr(st.identity_address)} target="_blank" rel="noreferrer">BscScan ↗</a>
          </div>
          <div style={{ marginTop: 6 }}>Balance <b>{bal.toFixed(4)} BNB</b> of {need} needed <span className="faint">· checking every 10 s</span></div>
          <div className="faint" style={{ marginTop: 2 }}>Send BNB on <b>BNB Smart Chain (BEP20)</b> only.</div>
        </div></div>}

      <Step n={3} title={`Register on ${netLabel}`} />
      {step === 3 && <div style={{ marginTop: 6 }}>
        {st.identity_address && <div className="small faint" style={{ marginBottom: 6 }}>Identity wallet <a className="mono" href={bscAddr(st.identity_address)} target="_blank" rel="noreferrer">{shortHex(st.identity_address)}</a> · {bal.toFixed(4)} BNB</div>}
        <label className="small" style={{ display: "flex", gap: 6, alignItems: "flex-start", marginBottom: 8 }}>
          <input type="checkbox" checked={confirm} onChange={(e) => setConfirm(e.target.checked)} />
          <span>I understand this sends a real transaction on BNB Chain mainnet, paid from the identity wallet's BNB.</span></label>
        <button className="btn" disabled={!confirm || busy != null} onClick={register}>{busy === "reg" ? <span className="spin" /> : "Register Regime on-chain"}</button>
      </div>}

      {err && <div className="banner red" style={{ marginTop: 10 }}><span className="grow small">{err}</span></div>}
    </div>
  );
}
