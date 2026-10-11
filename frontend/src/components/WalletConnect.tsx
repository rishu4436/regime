// "Connect wallet": Agentic Wallet QR sign-in from the dashboard.
// POST /api/wallet/connect -> {qr_url, session_id, expires_at} (backend accepts it from localhost only, else 403),
// then poll GET /api/wallet/connect?session_id= until connected | expired | failed. On "connected" the backend has
// already re-read the wallet limits; we refresh /api/wallet + the live gate and only then show "can trade".
import { createContext, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import qrcode from "qrcode-generator";
import { api } from "../api";
import { ApiError, type WalletConnectStart, type WalletConnectStatus } from "../api/types";
import { bscAddr, shortHex, useLive } from "../lib/live";
import { useToast } from "./ui";

function Qr({ text, size = 220 }: { text: string; size?: number }) {
  const src = useMemo(() => {
    if (/^data:image\//.test(text)) return text;      // backend may already send an image
    try { const q = qrcode(0, "M"); q.addData(text); q.make(); return q.createDataURL(Math.max(2, Math.floor(size / (q.getModuleCount() + 8))), 4 * 2); }
    catch { return null; }
  }, [text, size]);
  return src ? <img src={src} width={size} height={size} alt="Agentic Wallet sign-in QR code" style={{ background: "#fff", borderRadius: 12, padding: 8, imageRendering: "pixelated" }} />
    : <div className="small muted">Couldn't draw the QR. Open the link below on your phone instead.</div>;
}

type Phase = "idle" | "starting" | "pending" | "verifying" | "connected" | "expired" | "failed";
interface ConnectCtx { open: () => void }
const Ctx = createContext<ConnectCtx>({ open: () => {} });
export const useWalletConnect = () => useContext(Ctx);

export function WalletConnectProvider({ children }: { children: ReactNode }) {
  const { wallet, gate, refresh } = useLive();
  const toast = useToast();
  const [show, setShow] = useState(false);
  const [phase, setPhase] = useState<Phase>("idle");
  const [start, setStart] = useState<WalletConnectStart | null>(null);
  const [result, setResult] = useState<WalletConnectStatus | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [now, setNow] = useState(Date.now());
  const walletAtConnect = useRef<string | null>(null);

  const begin = async () => {
    setShow(true); setPhase("starting"); setErr(null); setResult(null); setStart(null);
    try { setStart(await api.startWalletConnect()); setPhase("pending"); }
    catch (e) {
      setPhase("failed");
      setErr(e instanceof ApiError && e.status === 403 ? "Wallet sign-in only works on the PC running the Regime backend. Open the dashboard there (http://localhost) and try again."
        : e instanceof ApiError && [404, 405, 501].includes(e.status) ? "This backend doesn't support signing in from the dashboard yet. Sign in on the host PC with baw auth signin."
        : e instanceof Error ? e.message : String(e));
    }
  };

  // poll the sign-in every 2 s while the QR is showing
  useEffect(() => {
    if (phase !== "pending" || !start) return;
    let stop = false;
    const id = setInterval(async () => {
      setNow(Date.now());
      try {
        const st = await api.getWalletConnect(start.session_id);
        if (stop) return;
        if (st.status === "connected") { setResult(st); walletAtConnect.current = wallet?.updated_at ?? null; setPhase("verifying"); refresh(); }
        else if (st.status === "expired" || st.status === "failed") { setResult(st); setPhase(st.status); }
      } catch (e) { if (e instanceof ApiError && e.status === 404) { setPhase("expired"); } }
    }, 2000);
    return () => { stop = true; clearInterval(id); };
  }, [phase, start]); // eslint-disable-line react-hooks/exhaustive-deps

  // Bot 3's rule: don't say "can trade" until the wallet (and its limits) has been fetched again after sign-in
  useEffect(() => {
    if (phase !== "verifying") return;
    const fresh = wallet?.connected && !wallet.session_expired && (wallet.updated_at ?? null) !== walletAtConnect.current;
    if (fresh) { setPhase("connected"); toast(result?.wallet_changed ? "Different wallet connected: live trading stays blocked until positions are checked" : "Wallet connected", result?.wallet_changed ? "info" : "success"); return; }
    const id = setInterval(refresh, 1500);
    return () => clearInterval(id);
  }, [phase, wallet?.updated_at, wallet?.connected]); // eslint-disable-line react-hooks/exhaustive-deps

  const left = start?.expires_at ? Math.max(0, Math.round((Date.parse(start.expires_at) - now) / 1000)) : null;
  const close = () => { setShow(false); if (phase !== "verifying") setPhase("idle"); };
  const addr = result?.address ?? wallet?.address ?? null;

  return (
    <Ctx.Provider value={{ open: begin }}>
      {children}
      {show && (
        <div className="modal-bg" onClick={close}>
          <div className="modal" onClick={(e) => e.stopPropagation()} role="dialog" aria-label="Connect Agentic Wallet">
            <h3>Connect your Binance Agentic Wallet</h3>
            {phase === "starting" && <div className="small muted"><span className="spin" /> Starting sign-in…</div>}
            {phase === "pending" && start && <>
              <div className="small muted" style={{ marginBottom: 12 }}>Open the <b>Binance App</b>, tap the scan icon and scan this code to let Regime trade from your Agentic Wallet. Funds stay in your wallet, inside the limits you set there.</div>
              <div style={{ display: "flex", justifyContent: "center", marginBottom: 10 }}><Qr text={start.qr_url} /></div>
              {start.pairing_code && <div className="small" style={{ textAlign: "center", marginBottom: 8 }}>Pairing code <b className="mono" style={{ fontSize: "1.05rem", letterSpacing: ".08em" }}>{start.pairing_code}</b><br /><span className="faint">Check it matches the code the Binance App shows.</span></div>}
              <div className="small faint" style={{ textAlign: "center" }}><span className="spin" /> Waiting for you to scan{left != null ? ` · expires in ${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}` : ""}</div>
              {!/^data:/.test(start.qr_url) && <div className="small" style={{ textAlign: "center", marginTop: 6 }}><a href={start.qr_url} target="_blank" rel="noreferrer">Open sign-in link</a></div>}
            </>}
            {phase === "verifying" && <div className="small"><span className="spin" /> Signed in. Re-reading the wallet's balances and limits before trading resumes…</div>}
            {phase === "connected" && <>
              {result?.wallet_changed
                ? <div className="banner amber" style={{ marginBottom: 10 }}><span className="grow small"><b>Different wallet connected.</b> Live trading stays blocked until you click <b>Check positions</b> in the banner, which matches the saved positions to this wallet.</span></div>
                : <div className="banner green" style={{ marginBottom: 10 }}><span className="grow small"><b>Wallet connected.</b> {gate?.can_trade ? "Regime can trade again." : "Limits refreshed. The live status panel shows anything still blocking trades."}</span></div>}
              {addr && <div className="kv"><span className="k">Address</span><a className="mono small" href={bscAddr(addr)} target="_blank" rel="noreferrer">{shortHex(addr)} ↗</a></div>}
            </>}
            {(phase === "expired" || phase === "failed") && <div className="banner red" style={{ marginBottom: 10 }}><span className="grow small">
              {phase === "expired" ? "The QR code expired before it was scanned." : err ?? result?.error ?? "Sign-in failed."}</span></div>}
            <div style={{ display: "flex", gap: 8, justifyContent: "flex-end", marginTop: 14 }}>
              {(phase === "expired" || phase === "failed") && <button className="btn" onClick={begin}>Try again</button>}
              <button className="btn ghost" onClick={close}>{phase === "connected" ? "Done" : "Close"}</button>
            </div>
          </div>
        </div>
      )}
    </Ctx.Provider>
  );
}

/** "Different wallet connected": LIVE places no orders until saved positions are checked against the new wallet. */
export function WalletChangedBanner() {
  const { gate, refresh } = useLive();
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const blocked = gate?.blocked_reasons?.some((r) => /different wallet connected/i.test(r));
  if (!blocked) return null;
  const run = async () => {
    setBusy(true);
    try {
      const r = await api.reconcileWallet();
      const kept = r.kept?.length ?? 0, dropped = r.dropped?.length ?? 0;
      toast(`Positions checked: ${kept} kept${dropped ? `, ${dropped} not in this wallet and removed` : ""}. Trading can resume.`, "success"); refresh();
    } catch (e) {
      toast(e instanceof ApiError && e.status === 409 ? "Can't check yet: an order is still settling or balances aren't available. Try again in a minute."
        : e instanceof ApiError && e.status === 403 ? "This check only works on the PC running the Regime backend." : e instanceof Error ? e.message : String(e), "error");
    } finally { setBusy(false); }
  };
  return (
    <div className="banner amber" role="alert"><span className="grow small"><b>Different wallet connected.</b> Regime places no orders, exits included, until its saved positions are checked against this wallet.</span>
      <button className="btn" style={{ padding: "5px 12px" }} disabled={busy} onClick={run}>{busy ? <span className="spin" /> : null}Check positions</button></div>
  );
}

/** Topbar button: "Connect wallet" / "Reconnect wallet" (session expired) / connected address. Shown in LIVE only. */
export function WalletButton() {
  const { wallet, isLive } = useLive();
  const { open } = useWalletConnect();
  if (!isLive) return null;
  const expired = !!wallet?.session_expired;
  if (wallet?.connected && !expired && wallet.address)
    return <button className="btn ghost" style={{ padding: "5px 10px" }} title="Agentic Wallet connected. Click to sign in again." onClick={open}>
      <span className="live-dot" style={{ marginRight: 6 }} /><span className="mono small">{shortHex(wallet.address, 4)}</span></button>;
  return <button className={expired ? "btn danger" : "btn"} style={{ padding: "5px 12px" }} onClick={open}>{expired ? "Reconnect wallet" : "Connect wallet"}</button>;
}
