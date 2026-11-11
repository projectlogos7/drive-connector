"use client";
import { Bell, CheckCircle2, Circle, FileText, Folder, FolderSync, HardDrive, Loader2, MoreHorizontal, Pause, Play, RefreshCw, Trash2, Users } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { mutate } from "swr";
import { del, post } from "@/lib/api";
import { ago, bytes, duration, fmt, useNow } from "@/lib/format";
import { useLive } from "@/lib/live";
import type { Progress, Source, SourceHealth, Stage } from "@/lib/types";
import { Button, Card, StatusBadge, cx, useConfirm, useToast } from "./ui";
import { AlertsDialog } from "./AlertsDialog";

const ICON = { my_drive: HardDrive, shared_drive: Users, folder: Folder, local_folder: Folder } as const;

export function ProgressBar({ percent, busy, indeterminate, tone = "accent" }: { percent: number; busy?: boolean; indeterminate?: boolean; tone?: "accent" | "ok" | "muted" }) {
  return (
    <div className="relative h-2.5 overflow-hidden rounded-full bg-track" role="progressbar" aria-valuemin={0} aria-valuemax={100}
      aria-valuenow={indeterminate ? undefined : percent}>
      {indeterminate ? (
        <div className="absolute inset-y-0 w-[35%] animate-indet rounded-full bg-accent" />
      ) : (
        <div className={cx("absolute inset-y-0 left-0 overflow-hidden rounded-full transition-[width] duration-700 ease-out",
          tone === "ok" ? "bg-ok" : tone === "muted" ? "bg-faint" : "bg-accent")} style={{ width: `${Math.max(percent, busy ? 2 : 0)}%` }}>
          {busy && <div className="absolute inset-0 animate-shimmer bg-gradient-to-r from-transparent via-white/35 to-transparent" />}
        </div>
      )}
    </div>
  );
}

function StageStep({ s, last }: { s: Stage; last: boolean }) {
  const pct = s.total ? Math.min(100, Math.round(((s.done || 0) / s.total) * 100)) : s.state === "done" ? 100 : 0;
  return (
    <div className={cx("min-w-0 flex-1", s.state === "skipped" && "opacity-40")}>
      <div className="flex items-center gap-1.5 text-xs font-medium">
        {s.state === "done" ? <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-ok" />
          : s.state === "active" ? <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin text-accent" />
          : <Circle className="h-3.5 w-3.5 shrink-0 text-faint" />}
        <span className={cx("truncate", s.state === "pending" ? "text-faint" : s.state === "active" ? "text-ink" : "text-muted")}>{s.label}</span>
      </div>
      <div className={cx("mt-1.5 h-1 rounded-full bg-track", !last && "mr-1")}>
        <div className={cx("h-1 rounded-full transition-[width] duration-700", s.state === "done" ? "bg-ok" : "bg-accent")}
          style={{ width: `${s.state === "done" ? 100 : pct}%` }} />
      </div>
      <div className="mt-1 truncate text-[11px] tabular-nums text-faint">
        {s.state === "skipped" ? "off" : s.state === "done" ? (s.total ? `${fmt(s.total)} ✓` : "done")
          : s.state === "active" ? (s.total ? `${fmt(s.done)} / ${fmt(s.total)}` : s.done ? `${fmt(s.done)} found` : "starting…")
          : "waiting"}
      </div>
    </div>
  );
}

/** Highlights the current file name briefly whenever it changes. */
function CurrentFile({ p }: { p: Progress }) {
  const c = p.current;
  const [key, setKey] = useState(0);
  const last = useRef<string | null>(null);
  useEffect(() => { if (c?.name && c.name !== last.current) { last.current = c.name; setKey((k) => k + 1); } }, [c?.name]);
  if (!c?.name) return null;
  return (
    <div key={key} className="flex min-w-0 items-center gap-2 rounded-lg bg-bg px-2.5 py-1.5 text-[13px] animate-flash">
      {c.kind === "folder" ? <Folder className="h-4 w-4 shrink-0 text-muted" /> : <FileText className="h-4 w-4 shrink-0 text-muted" />}
      <span className="shrink-0 text-muted">{c.action}</span>
      <span className="min-w-0 truncate font-medium" title={c.path || c.name}>{c.name}</span>
      {c.source && <span className="shrink-0 text-muted">· {c.source}</span>}
      {c.size != null && <span className="shrink-0 tabular-nums text-muted">· {bytes(c.size)}</span>}
    </div>
  );
}

export function SourceCard({ source, health, showOwner }: { source: Source; health?: SourceHealth; showOwner?: boolean }) {
  const live = useLive();
  const now = useNow(10000);
  const toast = useToast();
  const { confirm, node: confirmNode } = useConfirm();
  const [menu, setMenu] = useState(false);
  const [alerts, setAlerts] = useState(false);
  const [pending, setPending] = useState<string | null>(null);
  const p: Progress | undefined = live.progress[source.id];
  const rate = live.rates[source.id];
  const Icon = ICON[source.root_kind] || Folder;
  const status = p?.status || source.status;
  const busy = p?.busy ?? (status === "connecting" || status === "initial_scan");
  const paused = status === "paused";
  const active = p?.stages.find((s) => s.state === "active");
  const indeterminate = busy && (!p || (!active?.total && (p.percent ?? 0) === 0));

  async function act(a: "pause" | "resume" | "resync") {
    setPending(a);
    try {
      await post(`/api/sources/${source.id}/${a}`);
      toast({ pause: "Paused", resume: "Resumed — catching up on changes", resync: "Full re-scan started" }[a], "ok");
      mutate("/api/sources"); mutate("/api/status");
    } catch (e) { toast((e as Error).message, "err"); }
    finally { setPending(null); }
  }
  async function remove() {
    setMenu(false);
    const ok = await confirm("Remove from the index?",
      `“${source.name}” will stop being watched and its entries will be removed from the index. Nothing is deleted from ${source.type === "gdrive" ? "Google Drive" : "the computer"}.`, "Remove");
    if (!ok) return;
    try { await del(`/api/sources/${source.id}`); toast("Removed", "ok"); mutate("/api/sources"); mutate((k) => typeof k === "string" && k.startsWith("/api/connections")); }
    catch (e) { toast((e as Error).message, "err"); }
  }

  const files = p?.files ?? source.files, folders = p?.folders ?? source.folders;
  const searchable = p?.searchable;
  const meta: string[] = [];
  if (busy && active && active.key === "index" && active.done) meta.push(`${fmt(active.done)} indexed so far`);
  else {
    meta.push(`${fmt(files)} files`);
    if (p?.content_enabled !== false && searchable != null) {
      const pending = p.searchable_pending || 0;
      const failed = p.searchable_failed || 0;
      if (pending) meta.push(`${fmt(searchable)} searchable · ${fmt(pending)} reading`);
      else if (failed && searchable === 0) meta.push(`0 searchable · ${fmt(failed)} failed`);
      else meta.push(`${fmt(searchable)} searchable`);
    }
    meta.push(`${fmt(folders)} folders`, p?.bytes != null ? bytes(p.bytes) : source.total_size);
  }
  if (showOwner) meta.push(source.type === "gdrive" ? "Google Drive" : source.device || "Computer");

  return (
    <Card className="p-5">
      {confirmNode}
      <AlertsDialog open={alerts} onClose={() => setAlerts(false)} source={source} />
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex min-w-0 items-start gap-3">
          <div className="grid h-9 w-9 shrink-0 place-items-center rounded-lg bg-accent-soft text-accent"><Icon className="h-[18px] w-[18px]" /></div>
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2"><span className="truncate font-semibold">{source.name}</span><StatusBadge status={status} busy={busy} />
              {!!health?.queue.dead_letter && <span className="rounded-full bg-bad-soft px-2 py-0.5 text-xs font-semibold text-bad">{health.queue.dead_letter} failed</span>}
            </div>
            <div className="mt-0.5 truncate text-[13px] text-muted">{meta.join(" · ")}
              {source.type === "local" && <span className="font-mono"> · {source.root}</span>}</div>
          </div>
        </div>
        <div className="relative flex items-center gap-1.5">
          <Button size="sm" variant="ghost" loading={pending === (paused ? "resume" : "pause")} onClick={() => act(paused ? "resume" : "pause")}
            icon={paused ? <Play className="h-3.5 w-3.5" /> : <Pause className="h-3.5 w-3.5" />}>{paused ? "Resume" : "Pause"}</Button>
          <Button size="sm" variant="ghost" loading={pending === "resync"} onClick={() => act("resync")} icon={<RefreshCw className="h-3.5 w-3.5" />}>Re-scan</Button>
          <Button size="sm" variant="subtle" aria-label="More" onClick={() => setMenu((m) => !m)}><MoreHorizontal className="h-4 w-4" /></Button>
          {menu && (
            <div className="absolute right-0 top-9 z-20 w-44 rounded-xl border border-line bg-panel p-1 shadow-card animate-pop" onMouseLeave={() => setMenu(false)}>
              <button className="flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-sm hover:bg-bg" onClick={() => { setMenu(false); setAlerts(true); }}><Bell className="h-4 w-4" />Alert settings</button>
              <button className="flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-sm text-bad hover:bg-bad-soft" onClick={remove}><Trash2 className="h-4 w-4" />Remove</button>
            </div>
          )}
        </div>
      </div>

      <div className="mt-4">
        <div className="mb-1.5 flex items-baseline justify-between gap-3 text-sm">
          <span className={cx("min-w-0 sm:truncate", busy ? "font-medium" : "text-muted")}>
            {p?.step && busy ? <span className="mr-1.5 text-xs font-semibold uppercase tracking-wide text-faint">Step {p.step} of {p.steps}</span> : null}
            {p?.label || (busy ? "Starting…" : paused ? "Paused" : "Up to date — watching for changes")}
          </span>
          {busy && !indeterminate ? <span className="shrink-0 text-base font-semibold tabular-nums">{p?.percent ?? 0}%</span>
            : !busy && status === "live" ? <span className="shrink-0 text-[13px] text-muted">
              {health?.last_successful_sync ? `synced ${ago(health.last_successful_sync, now)}` : ""}
              {health?.push_channel ? (health.push_channel.active ? " · instant updates" : " · checks every few minutes") : ""}
              {health?.device ? (health.device.online ? " · computer online" : " · computer offline") : ""}</span> : null}
        </div>
        <ProgressBar percent={busy ? p?.percent ?? 0 : paused ? 0 : 100} busy={busy} indeterminate={indeterminate} tone={!busy && !paused ? "ok" : paused ? "muted" : "accent"} />
        {p && (busy || p.stages.some((s) => s.state === "active")) && (
          <>
            <div className="mt-4 grid grid-cols-2 gap-x-2 gap-y-3 sm:flex sm:gap-2">{p.stages.map((s, i) => <StageStep key={s.key} s={s} last={i === p.stages.length - 1} />)}</div>
            <div className="mt-3 flex flex-wrap items-center justify-between gap-2">
              <CurrentFile p={p} />
              {rate && rate.perSec > 0 && (
                <span className="text-[13px] tabular-nums text-muted">
                  {rate.etaSec != null && rate.etaSec > 1 ? `about ${duration(rate.etaSec)} left · ` : ""}
                  {rate.perSec >= 10 ? Math.round(rate.perSec) : rate.perSec.toFixed(1)} files/s
                </span>
              )}
            </div>
          </>
        )}
        {!busy && status !== "live" && source.status_reason && (
          <div className="mt-2 flex items-center gap-1.5 text-[13px] text-warn"><FolderSync className="h-3.5 w-3.5" />{source.status_reason}</div>
        )}
      </div>
    </Card>
  );
}
