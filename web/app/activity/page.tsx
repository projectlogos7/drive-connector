"use client";
import { useEffect, useMemo, useState } from "react";
import useSWR from "swr";
import { ChangeRow } from "@/components/ChangeRow";
import { Button, Card, ErrorNote, PageHead, Skeleton, cx } from "@/components/ui";
import { api } from "@/lib/api";
import { ago, useNow } from "@/lib/format";
import { useLive } from "@/lib/live";
import type { Change, Notification, Source } from "@/lib/types";

const TYPES = ["added", "modified", "renamed", "moved", "trashed", "restored", "deleted", "removed"];
type Page = { changes: Change[]; total: number; total_is_lower_bound: boolean; next_cursor: string | null };

export default function ActivityPage() {
  const live = useLive();
  const now = useNow(10000);
  const [tab, setTab] = useState<"changes" | "notifications">("changes");
  const [since, setSince] = useState("24h");
  const [source, setSource] = useState("");
  const [type, setType] = useState("");
  const { data: srcs } = useSWR<{ sources: Source[] }>("/api/sources");
  const q = new URLSearchParams({ since, limit: "100" });
  if (source) q.set("source", source);
  const key = `/api/changes?${q}`;
  const { data, error, isLoading, mutate } = useSWR<Page>(tab === "changes" ? key : null);
  const [more, setMore] = useState<Change[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  useEffect(() => { setMore([]); setCursor(data?.next_cursor ?? null); }, [data]);
  const { data: notes, isLoading: notesLoading } = useSWR<{ notifications: Notification[] }>(tab === "notifications" ? "/api/notifications?limit=100" : null);

  const srcName = srcs?.sources.find((s) => s.id === source)?.name;
  const rows = useMemo(() => {
    const seen = new Set<string>();
    const liveRows = live.changes.filter((c) => !srcName || c.source === srcName);
    return [...liveRows, ...(data?.changes || []), ...more].filter((c) => {
      const k = `${c.id ?? c.event_id}`; if (seen.has(k)) return false; seen.add(k);
      return !type || c.type === type;
    });
  }, [live.changes, data, more, type, srcName]);

  async function loadMore() {
    if (!cursor) return;
    setLoadingMore(true);
    try { const r = await api<Page>(`${key}&cursor=${encodeURIComponent(cursor)}`); setMore((m) => [...m, ...r.changes]); setCursor(r.next_cursor); }
    finally { setLoadingMore(false); }
  }

  return (
    <>
      <PageHead title="Activity" sub="Every change the index has seen, as it happens." />
      <Card className="p-5">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-3 border-b border-line">
          <div className="flex gap-1">
            {(["changes", "notifications"] as const).map((t) => (
              <button key={t} onClick={() => setTab(t)} className={cx("-mb-px border-b-2 px-3 py-2 text-sm capitalize",
                tab === t ? "border-accent font-semibold text-ink" : "border-transparent text-muted hover:text-ink")}>{t}</button>
            ))}
          </div>
          {tab === "changes" && (
            <div className="flex flex-wrap gap-2 pb-2">
              <Select value={since} onChange={setSince} options={[["1h", "Last hour"], ["24h", "Last 24 hours"], ["7d", "Last 7 days"], ["30d", "Last 30 days"]]} />
              <Select value={source} onChange={setSource} options={[["", "All sources"], ...(srcs?.sources.map((s) => [s.id, s.name] as [string, string]) || [])]} />
              <Select value={type} onChange={setType} options={[["", "All changes"], ...TYPES.map((t) => [t, t[0].toUpperCase() + t.slice(1)] as [string, string])]} />
            </div>
          )}
        </div>
        {tab === "changes" ? (
          error ? <ErrorNote error={error} retry={() => mutate()} /> : isLoading && !data ? <div className="space-y-3 py-2">{Array.from({ length: 6 }).map((_, i) => <Skeleton key={i} className="h-10 w-full" />)}</div>
            : rows.length ? <>
              <div className="mb-1 text-xs text-muted">{data ? `${data.total.toLocaleString()}${data.total_is_lower_bound ? "+" : ""} changes in this period` : ""}{live.connected ? " · new changes appear live" : ""}</div>
              {rows.map((c) => <ChangeRow key={`${c.id ?? c.event_id}`} c={c} now={now} fresh={!!c.id && live.newChangeIds.has(c.id)} />)}
              {cursor && <div className="mt-4 text-center"><Button variant="ghost" loading={loadingMore} onClick={loadMore}>Load more</Button></div>}
            </>
            : <p className="py-10 text-center text-sm text-muted">No changes in this period.</p>
        ) : notesLoading ? <Skeleton className="h-24 w-full" /> : notes?.notifications.length ? notes.notifications.map((n) => (
          <div key={n.id} className="border-b border-line py-3 last:border-0">
            <div className="flex justify-between gap-2"><b className="font-medium">{n.title}</b>
              <span className="shrink-0 text-xs text-muted">{n.kind === "alert" && <span className="mr-2 rounded-full bg-bad-soft px-2 py-0.5 font-semibold text-bad">alert</span>}{ago(n.created_at, now)}</span></div>
            <p className="mt-1 whitespace-pre-line text-sm text-muted">{n.body}</p>
          </div>
        )) : <p className="py-10 text-center text-sm text-muted">No notifications yet.</p>}
      </Card>
    </>
  );
}

function Select({ value, onChange, options }: { value: string; onChange: (v: string) => void; options: [string, string][] }) {
  return (
    <select value={value} onChange={(e) => onChange(e.target.value)} className="rounded-lg border border-line bg-panel px-2.5 py-1.5 text-sm">
      {options.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
    </select>
  );
}
