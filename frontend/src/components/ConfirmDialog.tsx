import type { ReactNode } from "react";

interface Props {
  title: string; children: ReactNode; confirmLabel?: string; danger?: boolean; busy?: boolean;
  onConfirm: () => void; onCancel: () => void;
}

export function ConfirmDialog({ title, children, confirmLabel = "Confirm", danger, busy, onConfirm, onCancel }: Props) {
  return (
    <div className="modal-bg" role="dialog" aria-modal="true" onClick={onCancel}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <h3>{title}</h3>
        <div style={{ fontSize: "0.88rem", color: "var(--muted)" }}>{children}</div>
        <div className="btn-row" style={{ marginTop: 16, justifyContent: "flex-end" }}>
          <button className="btn ghost" onClick={onCancel} disabled={busy}>Cancel</button>
          <button className={`btn${danger ? " red" : ""}`} onClick={onConfirm} disabled={busy} autoFocus>{busy ? "Working…" : confirmLabel}</button>
        </div>
      </div>
    </div>
  );
}
