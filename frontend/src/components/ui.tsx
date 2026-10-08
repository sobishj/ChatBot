// Small UI kit: buttons, fields, cards, badges, modal, tabs, toasts, empty states.
import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { Icon } from "./icons";

// ---------------------------------------------------------------- toast
type ToastItem = { id: number; text: string; kind: "info" | "error" };
const ToastCtx = createContext<(text: string, kind?: "info" | "error") => void>(() => undefined);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const push = useCallback((text: string, kind: "info" | "error" = "info") => {
    const id = Date.now() + Math.random();
    setItems((xs) => [...xs, { id, text, kind }]);
    setTimeout(() => setItems((xs) => xs.filter((x) => x.id !== id)), kind === "error" ? 6000 : 3500);
  }, []);
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="toasts" role="status" aria-live="polite">
        {items.map((t) => (
          <div key={t.id} className={`toast ${t.kind === "error" ? "error" : ""}`}>{t.text}</div>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}

export const useToast = () => useContext(ToastCtx);

// ---------------------------------------------------------------- basics
export function Button({
  children, kind = "default", size, loading, icon, ...rest
}: React.ButtonHTMLAttributes<HTMLButtonElement> & { kind?: "default" | "primary" | "danger" | "ghost"; size?: "sm"; loading?: boolean; icon?: string }) {
  return (
    <button type="button" {...rest} className={`btn ${kind !== "default" ? kind : ""} ${size ?? ""} ${rest.className ?? ""}`} disabled={rest.disabled || loading}>
      {loading ? <span className="spinner" /> : icon ? <Icon name={icon} /> : null}
      {children}
    </button>
  );
}

export function Card({ title, subtitle, actions, children, bodyless }: { title?: ReactNode; subtitle?: ReactNode; actions?: ReactNode; children?: ReactNode; bodyless?: boolean }) {
  return (
    <section className="card">
      {(title || actions) && (
        <div className="card-head">
          <div>
            {title && <h2>{title}</h2>}
            {subtitle && <p>{subtitle}</p>}
          </div>
          {actions && <div className="row">{actions}</div>}
        </div>
      )}
      {bodyless ? children : <div className="card-body">{children}</div>}
    </section>
  );
}

export function Field({ label, hint, children, full, htmlFor }: { label: ReactNode; hint?: ReactNode; children: ReactNode; full?: boolean; htmlFor?: string }) {
  return (
    <div className={`field ${full ? "full" : ""}`}>
      <label htmlFor={htmlFor}>{label}</label>
      {children}
      {hint && <div className="hint">{hint}</div>}
    </div>
  );
}

export function Alert({ kind = "info", children }: { kind?: "info" | "error" | "success" | "warning"; children: ReactNode }) {
  const icon = kind === "success" ? "check" : kind === "info" ? "info" : "alert";
  return (
    <div className={`alert ${kind}`} role={kind === "error" ? "alert" : undefined}>
      <Icon name={icon} />
      <div>{children}</div>
    </div>
  );
}

export function Badge({ color, children }: { color?: "green" | "red" | "amber" | "blue" | "teal"; children: ReactNode }) {
  return <span className={`badge ${color ?? ""}`}>{children}</span>;
}

const STATUS: Record<string, { color: "green" | "red" | "amber" | "blue" | undefined; label: string }> = {
  queued: { color: undefined, label: "Queued" },
  running: { color: "blue", label: "Running" },
  succeeded: { color: "green", label: "Succeeded" },
  failed: { color: "red", label: "Failed" },
  cancelled: { color: "amber", label: "Cancelled" },
  indexed: { color: "green", label: "Indexed" },
  pending: { color: "blue", label: "Pending" },
  error: { color: "red", label: "Error" },
};

export function StatusBadge({ status }: { status: string }) {
  const s = STATUS[status] ?? { color: undefined, label: status };
  return <Badge color={s.color}>{s.color === "blue" && status === "running" ? <span className="spinner" style={{ width: 10, height: 10 }} /> : <span className="dot" />}{s.label}</Badge>;
}

export function Progress({ value }: { value: number }) {
  return (
    <div className="progress" role="progressbar" aria-valuenow={value} aria-valuemin={0} aria-valuemax={100}>
      <div style={{ width: `${Math.max(2, Math.min(100, value))}%` }} />
    </div>
  );
}

export function Spinner() {
  return <span className="spinner" aria-label="Loading" />;
}

export function Loading({ text = "Loading…" }: { text?: string }) {
  return <div className="empty"><Spinner /> <span style={{ marginLeft: 8 }}>{text}</span></div>;
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="empty">
      <h3>{title}</h3>
      {children}
    </div>
  );
}

export function Stat({ label, value, sub }: { label: string; value: ReactNode; sub?: ReactNode }) {
  return (
    <div className="card stat">
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
      {sub && <div className="stat-sub">{sub}</div>}
    </div>
  );
}

// ---------------------------------------------------------------- modal
export function Modal({ title, onClose, children, footer, wide }: { title: string; onClose: () => void; children: ReactNode; footer?: ReactNode; wide?: boolean }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className={`modal ${wide ? "wide" : ""}`} role="dialog" aria-modal="true" aria-label={title}>
        <div className="modal-head">
          <h2>{title}</h2>
          <button className="btn ghost icon-btn" onClick={onClose} aria-label="Close"><Icon name="x" /></button>
        </div>
        <div className="modal-body">{children}</div>
        {footer && <div className="modal-foot">{footer}</div>}
      </div>
    </div>
  );
}

export function Tabs<T extends string>({ tabs, value, onChange }: { tabs: { key: T; label: string }[]; value: T; onChange: (key: T) => void }) {
  return (
    <div className="tabs" role="tablist">
      {tabs.map((t) => (
        <button key={t.key} role="tab" aria-selected={value === t.key} className={`tab ${value === t.key ? "active" : ""}`} onClick={() => onChange(t.key)}>
          {t.label}
        </button>
      ))}
    </div>
  );
}

export function Segmented<T extends string | number>({ options, value, onChange }: { options: { value: T; label: string }[]; value: T; onChange: (v: T) => void }) {
  return (
    <div className="segmented" role="group">
      {options.map((o) => (
        <button key={String(o.value)} type="button" className={o.value === value ? "active" : ""} aria-pressed={o.value === value} onClick={() => onChange(o.value)}>
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function CopyButton({ text, label = "Copy" }: { text: string; label?: string }) {
  const [done, setDone] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      const ta = document.createElement("textarea");
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      ta.remove();
    }
    setDone(true);
    setTimeout(() => setDone(false), 1800);
  };
  return <Button icon={done ? "check" : "copy"} onClick={copy}>{done ? "Copied" : label}</Button>;
}

/** Wraps an async action with loading state and toast-on-error. */
export function useAction() {
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const run = useCallback(
    async <T,>(fn: () => Promise<T>, success?: string): Promise<T | undefined> => {
      setBusy(true);
      try {
        const result = await fn();
        if (success) toast(success);
        return result;
      } catch (e) {
        toast((e as Error).message, "error");
        return undefined;
      } finally {
        setBusy(false);
      }
    },
    [toast],
  );
  return { busy, run };
}
