"use client";
import { ChevronRight, Folder, HardDrive, Loader2, Users, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { mutate } from "swr";
import { api, put } from "@/lib/api";
import type { Root } from "@/lib/types";
import { Button, Dialog, cx, useToast } from "./ui";

export async function connectDrive(content?: boolean, connectionId?: string) {
  const q = new URLSearchParams();
  if (content) q.set("content", "true");
  if (connectionId) q.set("connection_id", connectionId);
  const r = await api<{ url: string }>(`/api/oauth/google/start${q.toString() ? `?${q}` : ""}`);
  window.location.href = r.url;
}

type FolderItem = { kind: "folder"; id: string; name: string };

function FolderNode({ conn, f, depth, picked, covered, onPick }: {
  conn: string; f: FolderItem; depth: number; picked: Map<string, FolderItem>; covered: boolean; onPick: (f: FolderItem, on: boolean, descendants: string[]) => void;
}) {
  const [open, setOpen] = useState(false);
  const [kids, setKids] = useState<FolderItem[] | null>(null);
  const [loading, setLoading] = useState(false);
  const checked = covered || picked.has(f.id);
  async function toggle() {
    const next = !open; setOpen(next);
    if (next && kids === null) {
      setLoading(true);
      try { setKids((await api<{ folders: FolderItem[] }>(`/api/connections/${conn}/roots?parent=${encodeURIComponent(f.id)}`)).folders); }
      finally { setLoading(false); }
    }
  }
  return (
    <div>
      <div className="flex items-center gap-1 rounded-md px-1 py-1 hover:bg-bg" style={{ paddingLeft: depth * 18 + 4 }}>
        <button type="button" onClick={toggle} aria-label={open ? "Collapse" : "Expand"} className="grid h-6 w-6 place-items-center rounded text-muted hover:text-ink">
          {loading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <ChevronRight className={cx("h-3.5 w-3.5 transition-transform", open && "rotate-90")} />}
        </button>
        <label className={cx("flex min-w-0 flex-1 cursor-pointer items-center gap-2 text-sm", covered && "opacity-60")} title={covered ? "Included because a parent folder is selected" : undefined}>
          <input type="checkbox" className="h-4 w-4 accent-[rgb(var(--accent))]" checked={checked} disabled={covered}
            onChange={(e) => onPick(f, e.target.checked, (kids || []).map((k) => k.id))} />
          <Folder className="h-4 w-4 shrink-0 text-muted" /><span className="truncate">{f.name}</span>
        </label>
      </div>
      {open && kids && (kids.length ? kids.map((k) => (
        <FolderNode key={k.id} conn={conn} f={k} depth={depth + 1} picked={picked} covered={checked} onPick={onPick} />
      )) : <div className="py-1 text-xs text-faint" style={{ paddingLeft: (depth + 1) * 18 + 34 }}>No folders inside</div>)}
    </div>
  );
}

function Choice({ value, mode, setMode, title, sub, children }: {
  value: "whole" | "folders"; mode: string; setMode: (m: "whole" | "folders") => void; title: string; sub: string; children?: React.ReactNode;
}) {
  return (
    <div className={cx("rounded-xl border-[1.5px] p-3.5 transition-colors", mode === value ? "border-accent bg-accent-soft/60" : "border-line hover:border-faint")}>
      <label className="flex cursor-pointer items-start gap-3">
        <input type="radio" name="mode" className="mt-1 accent-[rgb(var(--accent))]" checked={mode === value} onChange={() => setMode(value)} />
        <div><div className="font-semibold">{title}</div><div className="text-[13px] text-muted">{sub}</div></div>
      </label>
      {children}
    </div>
  );
}

export function SelectionDialog({ conn, email, open, onClose, justConnected }: { conn: string; email?: string | null; open: boolean; onClose: () => void; justConnected?: boolean }) {
  const toast = useToast();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [myRoot, setMyRoot] = useState<Root | null>(null);
  const [shared, setShared] = useState<Root[]>([]);
  const [top, setTop] = useState<FolderItem[] | null>(null);
  const [mode, setMode] = useState<"whole" | "folders">("whole");
  const [picked, setPicked] = useState<Map<string, FolderItem>>(new Map());
  const [drives, setDrives] = useState<Set<string>>(new Set());
  const [initial, setInitial] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!open) return;
    let cancel = false;
    setLoading(true); setError(null); setTop(null);
    Promise.all([api(`/api/connections/${conn}/roots`), api<{ roots: Root[] }>(`/api/connections/${conn}/selection`)])
      .then(([roots, sel]) => {
        if (cancel) return;
        setMyRoot(roots.my_drive); setShared(roots.shared_drives);
        const cur = sel.roots;
        setMode(cur.length === 0 || cur.some((r) => r.kind === "my_drive") ? "whole" : "folders");
        setPicked(new Map(cur.filter((r) => r.kind === "folder").map((r) => [r.id, { kind: "folder", id: r.id, name: r.name }])));
        setDrives(new Set(cur.filter((r) => r.kind === "shared_drive").map((r) => r.id)));
        setInitial(cur.map((r) => r.id));
      })
      .catch((e) => !cancel && setError(e.message))
      .finally(() => !cancel && setLoading(false));
    return () => { cancel = true; };
  }, [open, conn]);

  useEffect(() => {
    if (open && mode === "folders" && top === null && myRoot) {
      api<{ folders: FolderItem[] }>(`/api/connections/${conn}/roots?parent=${encodeURIComponent(myRoot.id)}`).then((r) => setTop(r.folders)).catch((e) => setError(e.message));
    }
  }, [open, mode, top, myRoot, conn]);

  const onPick = useCallback((f: FolderItem, on: boolean, descendants: string[]) => {
    setPicked((m) => { const n = new Map(m); if (on) { n.set(f.id, f); descendants.forEach((d) => n.delete(d)); } else n.delete(f.id); return n; });
  }, []);

  const roots: Root[] = useMemo(() => {
    const r: Root[] = mode === "whole" && myRoot ? [{ kind: "my_drive", id: myRoot.id, name: "My Drive" }] : mode === "folders" ? [...picked.values()] : [];
    return r.concat(shared.filter((d) => drives.has(d.id)).map((d) => ({ kind: "shared_drive" as const, id: d.id, name: d.name })));
  }, [mode, myRoot, picked, shared, drives]);
  const dropping = initial.filter((id) => !roots.some((r) => r.id === id)).length;
  const summary = [mode === "whole" ? "Whole My Drive" : `${picked.size} folder${picked.size === 1 ? "" : "s"}`,
    drives.size ? `${drives.size} shared drive${drives.size === 1 ? "" : "s"}` : ""].filter(Boolean).join(" + ");

  async function save() {
    setSaving(true);
    try {
      const r = await put<{ added: { name: string }[]; removed: { name: string }[] }>(`/api/connections/${conn}/selection`, { roots });
      toast(r.added.length ? `Indexing started: ${r.added.map((a) => a.name).join(", ")}` : "Saved", "ok");
      mutate("/api/sources"); mutate(`/api/connections/${conn}/selection`); onClose();
    } catch (e) { toast((e as Error).message, "err"); } finally { setSaving(false); }
  }

  return (
    <Dialog open={open} onClose={onClose} wide title={`What should we watch${email ? ` in ${email}` : ""}?`}
      sub={justConnected ? "Your whole My Drive is already being indexed. Keep it, or pick specific folders." : "Changes apply straight away. Nothing in Google Drive is ever modified."}
      footer={<><span className="text-[13px] text-muted">{summary}</span>
        <div className="flex gap-2"><Button variant="ghost" onClick={onClose}>{justConnected ? "Keep whole My Drive" : "Cancel"}</Button>
          <Button loading={saving} disabled={!roots.length || loading} onClick={save}>Save</Button></div></>}>
      {loading ? <div className="space-y-3 py-2">{[0, 1, 2].map((i) => <div key={i} className="h-16 animate-pulse rounded-xl bg-track" />)}</div>
        : error ? <p className="text-sm text-bad">{error}</p> : (
          <div className="space-y-3 pb-1">
            <div className="text-xs font-semibold uppercase tracking-wide text-faint">My Drive</div>
            <Choice value="whole" mode={mode} setMode={setMode} title="Whole My Drive" sub="Every file and folder in My Drive, plus files shared with you. Recommended." />
            <Choice value="folders" mode={mode} setMode={setMode} title="Only specific folders" sub="Pick folders; everything inside them is included.">
              {mode === "folders" && (
                <div className="mt-3">
                  <div className="max-h-72 overflow-auto rounded-lg border border-line bg-panel p-1">
                    {top === null ? <div className="flex items-center gap-2 p-3 text-sm text-muted"><Loader2 className="h-4 w-4 animate-spin" />Loading folders…</div>
                      : top.length ? top.map((f) => <FolderNode key={f.id} conn={conn} f={f} depth={0} picked={picked} covered={false} onPick={onPick} />)
                      : <p className="p-3 text-sm text-muted">No folders in My Drive.</p>}
                  </div>
                  {picked.size > 0 && <div className="mt-2 flex flex-wrap gap-1.5">{[...picked.values()].map((f) => (
                    <span key={f.id} className="inline-flex items-center gap-1 rounded-full bg-accent-soft py-0.5 pl-2.5 pr-1 text-[13px]">
                      <Folder className="h-3.5 w-3.5" />{f.name}
                      <button aria-label={`Remove ${f.name}`} className="rounded-full p-0.5 text-muted hover:text-ink" onClick={() => onPick(f, false, [])}><X className="h-3.5 w-3.5" /></button>
                    </span>))}</div>}
                </div>
              )}
            </Choice>
            {shared.length > 0 && <>
              <div className="pt-2 text-xs font-semibold uppercase tracking-wide text-faint">Shared drives</div>
              {shared.map((d) => (
                <label key={d.id} className={cx("flex cursor-pointer items-center gap-3 rounded-xl border-[1.5px] p-3.5", drives.has(d.id) ? "border-accent bg-accent-soft/60" : "border-line")}>
                  <input type="checkbox" className="h-4 w-4 accent-[rgb(var(--accent))]" checked={drives.has(d.id)}
                    onChange={(e) => setDrives((s) => { const n = new Set(s); e.target.checked ? n.add(d.id) : n.delete(d.id); return n; })} />
                  <Users className="h-4 w-4 text-muted" /><span className="font-medium">{d.name}</span><span className="text-[13px] text-muted">whole shared drive</span>
                </label>))}
            </>}
            {dropping > 0 && <div className="rounded-lg bg-warn-soft px-3 py-2 text-[13px] text-warn">
              {dropping} location{dropping > 1 ? "s" : ""} currently watched will stop being watched and {dropping > 1 ? "their" : "its"} entries will be removed from the index.</div>}
          </div>
        )}
    </Dialog>
  );
}

export const DriveIcon = HardDrive;
