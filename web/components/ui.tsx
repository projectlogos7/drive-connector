"use client";
import { Check, Copy, Loader2, X } from "lucide-react";
import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";

export function cx(...c: (string | false | null | undefined)[]) { return c.filter(Boolean).join(" "); }

type BtnProps = React.ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "ghost" | "danger" | "subtle"; size?: "sm" | "md"; loading?: boolean; icon?: React.ReactNode;
};
export function Button({ variant = "primary", size = "md", loading, icon, className, children, disabled, ...rest }: BtnProps) {
  return (
    <button
      {...rest}
      disabled={disabled || loading}
      className={cx(
        "inline-flex items-center justify-center gap-1.5 rounded-lg font-medium transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent disabled:opacity-50 disabled:cursor-not-allowed whitespace-nowrap",
        size === "sm" ? "px-2.5 py-1 text-[13px]" : "px-3.5 py-2 text-sm",
        variant === "primary" && "bg-accent text-accent-ink hover:brightness-110",
        variant === "ghost" && "border border-line bg-panel text-ink hover:bg-bg",
        variant === "subtle" && "text-muted hover:bg-bg hover:text-ink",
        variant === "danger" && "border border-line bg-panel text-bad hover:bg-bad-soft",
        className,
      )}
    >
      {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : icon}
      {children}
    </button>
  );
}

export function Card({ className, children }: { className?: string; children: React.ReactNode }) {
  return <div className={cx("rounded-xl border border-line bg-panel shadow-card", className)}>{children}</div>;
}

const STATUS_STYLE: Record<string, string> = {
  live: "bg-ok-soft text-ok", initial_scan: "bg-warn-soft text-warn", connecting: "bg-warn-soft text-warn",
  degraded: "bg-warn-soft text-warn", error: "bg-bad-soft text-bad", paused: "bg-bg text-muted",
};
const STATUS_TEXT: Record<string, string> = {
  live: "Live", initial_scan: "Indexing", connecting: "Starting", degraded: "Degraded", error: "Error", paused: "Paused",
};
export function StatusBadge({ status, busy }: { status: string; busy?: boolean }) {
  const s = busy && status === "live" ? "initial_scan" : status;
  const text = busy && status === "live" ? "Working" : STATUS_TEXT[status] || status;
  return (
    <span className={cx("inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-semibold", STATUS_STYLE[s] || "bg-bg text-muted")}>
      <span className={cx("h-1.5 w-1.5 rounded-full bg-current", (s === "initial_scan" || s === "connecting") && "animate-pulse")} />
      {text}
    </span>
  );
}

export function Skeleton({ className }: { className?: string }) {
  return <div className={cx("relative overflow-hidden rounded-md bg-track", className)}>
    <div className="absolute inset-0 -translate-x-full animate-shimmer bg-gradient-to-r from-transparent via-white/30 to-transparent" />
  </div>;
}

export function CardSkeleton({ rows = 2 }: { rows?: number }) {
  return (
    <Card className="p-5">
      <Skeleton className="h-5 w-48" />
      <Skeleton className="mt-2 h-3.5 w-72 max-w-full" />
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="mt-5 space-y-2"><Skeleton className="h-4 w-40" /><Skeleton className="h-2.5 w-full" /><Skeleton className="h-3 w-56" /></div>
      ))}
    </Card>
  );
}

export function Empty({ icon, title, children, action }: { icon: React.ReactNode; title: string; children?: React.ReactNode; action?: React.ReactNode }) {
  return (
    <Card className="px-6 py-12 text-center">
      <div className="mx-auto grid h-12 w-12 place-items-center rounded-full bg-accent-soft text-accent">{icon}</div>
      <h2 className="mt-4 text-lg font-semibold">{title}</h2>
      {children && <p className="mx-auto mt-1.5 max-w-md text-sm text-muted">{children}</p>}
      {action && <div className="mt-5 flex flex-wrap justify-center gap-2">{action}</div>}
    </Card>
  );
}

export function PageHead({ title, sub, actions }: { title: string; sub?: string; actions?: React.ReactNode }) {
  return (
    <div className="mb-6 flex flex-wrap items-end justify-between gap-4">
      <div><h1 className="text-2xl font-semibold tracking-tight">{title}</h1>{sub && <p className="mt-1 text-sm text-muted">{sub}</p>}</div>
      {actions && <div className="flex flex-wrap gap-2">{actions}</div>}
    </div>
  );
}

export function ErrorNote({ error, retry }: { error: unknown; retry?: () => void }) {
  return (
    <Card className="flex flex-wrap items-center justify-between gap-3 border-bad/30 bg-bad-soft p-4 text-sm text-bad">
      <span>Couldn’t load this: {(error as Error)?.message || "unknown error"}</span>
      {retry && <Button size="sm" variant="ghost" onClick={retry}>Try again</Button>}
    </Card>
  );
}

/* ---------- dialog ---------- */
export function Dialog({ open, onClose, title, sub, children, footer, wide }: {
  open: boolean; onClose: () => void; title: string; sub?: React.ReactNode; children: React.ReactNode; footer?: React.ReactNode; wide?: boolean;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  useEffect(() => {
    if (!open) return;
    const prev = document.activeElement as HTMLElement | null;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") closeRef.current(); };
    document.addEventListener("keydown", onKey);
    // Focus the first field if there is one; otherwise the dialog itself (not the close button).
    setTimeout(() => (ref.current?.querySelector<HTMLElement>("input:not([type=radio]):not([type=checkbox]),textarea,select") || ref.current)?.focus(), 30);
    document.body.style.overflow = "hidden";
    return () => { document.removeEventListener("keydown", onKey); document.body.style.overflow = ""; prev?.focus?.(); };
  }, [open]);
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-black/45 p-4" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div ref={ref} role="dialog" aria-modal="true" aria-label={title} tabIndex={-1}
        className={cx("flex max-h-[88vh] w-full flex-col rounded-2xl bg-panel shadow-2xl outline-none animate-pop", wide ? "max-w-2xl" : "max-w-lg")}>
        <div className="flex items-start justify-between gap-3 px-6 pb-2 pt-5">
          <div><h2 className="text-lg font-semibold">{title}</h2>{sub && <div className="mt-1 text-sm text-muted">{sub}</div>}</div>
          <button onClick={onClose} aria-label="Close" className="rounded-md p-1 text-muted hover:bg-bg hover:text-ink"><X className="h-5 w-5" /></button>
        </div>
        <div className="flex-1 overflow-auto px-6 py-3">{children}</div>
        {footer && <div className="flex flex-wrap items-center justify-between gap-2 border-t border-line px-6 py-4">{footer}</div>}
      </div>
    </div>
  );
}

export function useConfirm() {
  const [st, setSt] = useState<{ title: string; text: string; ok: string; resolve: (v: boolean) => void } | null>(null);
  const confirm = useCallback((title: string, text: string, ok = "Confirm") =>
    new Promise<boolean>((resolve) => setSt({ title, text, ok, resolve })), []);
  const close = (v: boolean) => { st?.resolve(v); setSt(null); };
  const node = (
    <Dialog open={!!st} onClose={() => close(false)} title={st?.title || ""} sub={st?.text}
      footer={<><span /><div className="flex gap-2"><Button variant="ghost" onClick={() => close(false)}>Cancel</Button>
        <Button variant={/remove|revoke|disconnect|delete/i.test(st?.ok || "") ? "danger" : "primary"} onClick={() => close(true)}>{st?.ok}</Button></div></>}>
      <span />
    </Dialog>
  );
  return { confirm, node };
}

/* ---------- toasts ---------- */
type Toast = { id: number; text: string; tone: "ok" | "err" | "info" };
const ToastCtx = createContext<(text: string, tone?: Toast["tone"]) => void>(() => {});
export const useToast = () => useContext(ToastCtx);
export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [items, setItems] = useState<Toast[]>([]);
  const push = useCallback((text: string, tone: Toast["tone"] = "info") => {
    const id = Date.now() + Math.random();
    setItems((x) => [...x, { id, text, tone }]);
    setTimeout(() => setItems((x) => x.filter((t) => t.id !== id)), tone === "err" ? 7000 : 4200);
  }, []);
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="pointer-events-none fixed left-1/2 top-4 z-[60] flex w-[min(420px,calc(100%-32px))] -translate-x-1/2 flex-col gap-2" aria-live="polite">
        {items.map((t) => (
          <div key={t.id} className={cx("pointer-events-auto rounded-xl px-4 py-2.5 text-sm shadow-lg animate-pop",
            t.tone === "err" ? "bg-bad text-white" : "bg-ink text-bg")}>{t.text}</div>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}

export function CopyBox({ text, label }: { text: string; label?: string }) {
  const [done, setDone] = useState(false);
  return (
    <div className="mt-2">
      {label && <div className="mb-1 text-xs font-semibold uppercase tracking-wide text-faint">{label}</div>}
      <div className="group relative rounded-lg border border-line bg-bg">
        <pre className="overflow-x-auto whitespace-pre-wrap break-all p-3 pr-11 font-mono text-[12.5px] leading-relaxed">{text}</pre>
        <button aria-label="Copy" className="absolute right-2 top-2 rounded-md border border-line bg-panel p-1.5 text-muted hover:text-ink"
          onClick={async () => { try { await navigator.clipboard.writeText(text); setDone(true); setTimeout(() => setDone(false), 1500); } catch { /* clipboard blocked */ } }}>
          {done ? <Check className="h-3.5 w-3.5 text-ok" /> : <Copy className="h-3.5 w-3.5" />}
        </button>
      </div>
    </div>
  );
}
