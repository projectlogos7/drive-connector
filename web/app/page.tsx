"use client";
import { HardDrive, Radio } from "lucide-react";
import Link from "next/link";
import useSWR from "swr";
import { ChangeRow } from "@/components/ChangeRow";
import { SourceCard } from "@/components/SourceCard";
import { Button, Card, CardSkeleton, Empty, ErrorNote, PageHead, Skeleton } from "@/components/ui";
import { bytes, fmt, useNow, ago } from "@/lib/format";
import { useLive } from "@/lib/live";
import type { Change, Notification, Source, SourceHealth } from "@/lib/types";
import { connectDrive } from "@/components/drive";

export default function Overview() {
  const live = useLive();
  const now = useNow(10000);
  const { data, error, mutate: reload } = useSWR<{ sources: Source[] }>("/api/sources", { refreshInterval: 30000 });
  const { data: status } = useSWR<{ sources: SourceHealth[] }>("/api/status", { refreshInterval: 30000 });
  const { data: recent } = useSWR<{ changes: Change[]; total: number; total_is_lower_bound: boolean }>("/api/changes?since=24h&limit=15", { refreshInterval: 20000 });
  const { data: notes } = useSWR<{ notifications: Notification[] }>("/api/notifications?limit=6");
  const health = Object.fromEntries((status?.sources || []).map((s) => [s.source_id, s]));
  const progress = Object.values(live.progress);
  const files = progress.length ? progress.reduce((a, p) => a + (p.files || 0), 0) : data?.sources.reduce((a, s) => a + s.files, 0);
  const folders = progress.length ? progress.reduce((a, p) => a + (p.folders || 0), 0) : data?.sources.reduce((a, s) => a + s.folders, 0);
  const searchable = progress.length ? progress.reduce((a, p) => a + (p.searchable || 0), 0) : null;
  const searchablePending = progress.reduce((a, p) => a + (p.searchable_pending || 0), 0);
  const size = progress.reduce((a, p) => a + (p.bytes || 0), 0);
  const working = progress.filter((p) => p.busy).length;
  // Live feed: stream items first, then the recent list (deduplicated)
  const seen = new Set<string>();
  const feed = [...live.changes, ...(recent?.changes || [])].filter((c) => {
    const k = `${c.id ?? c.event_id}`; if (seen.has(k)) return false; seen.add(k); return true;
  }).slice(0, 12);
  // Count from the server, plus changes streamed in since that count was taken.
  const newestCounted = Math.max(0, ...(recent?.changes || []).map((c) => c.event_id || 0));
  const today = (recent?.total ?? 0) + live.changes.filter((c) => (c.id || 0) > newestCounted).length;

  return (
    <>
      <PageHead title="Overview" sub="Everything being indexed, live." />
      <div className="mb-6 grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Tile label="Files indexed" value={files == null ? null : fmt(files)}
          sub={searchable == null ? undefined : searchablePending ? `${fmt(searchable)} searchable · ${fmt(searchablePending)} reading` : `${fmt(searchable)} searchable`} />
        <Tile label="Folders" value={folders == null ? null : fmt(folders)} sub={size ? bytes(size) : undefined} />
        <Tile label="Sources" value={data ? `${data.sources.length}` : null} sub={working ? `${working} working now` : data?.sources.length ? "all up to date" : undefined} />
        <Tile label="Changes in 24 h" value={recent ? `${fmt(today)}${recent.total_is_lower_bound ? "+" : ""}` : null} />
      </div>

      {error ? <ErrorNote error={error} retry={() => reload()} /> : !data ? (
        <div className="space-y-4"><CardSkeleton /><CardSkeleton /></div>
      ) : data.sources.length === 0 ? (
        <Empty icon={<HardDrive className="h-5 w-5" />} title="Nothing connected yet"
          action={<><Button onClick={() => connectDrive()}>Connect Google Drive</Button><Link href="/computers/"><Button variant="ghost">Add a computer</Button></Link></>}>
          Connect a Google account or add a computer; indexing starts straight away and you can watch it here.
        </Empty>
      ) : (
        <div className="space-y-4">{data.sources.map((s) => <SourceCard key={s.id} source={s} health={health[s.id]} showOwner />)}</div>
      )}

      <div className="mt-6 grid gap-4 lg:grid-cols-[1.4fr_1fr]">
        <Card className="p-5">
          <div className="mb-2 flex items-center justify-between"><h2 className="flex items-center gap-2 font-semibold"><Radio className="h-4 w-4 text-ok" />Live activity</h2>
            <Link href="/activity/" className="text-sm text-accent hover:underline">See all</Link></div>
          {!recent ? <div className="space-y-3 py-2">{[0, 1, 2].map((i) => <Skeleton key={i} className="h-9 w-full" />)}</div>
            : feed.length ? feed.map((c) => <ChangeRow key={`${c.id ?? c.event_id}`} c={c} now={now} fresh={!!c.id && live.newChangeIds.has(c.id)} />)
            : <p className="py-6 text-center text-sm text-muted">No changes yet. New, renamed and moved files appear here the moment they’re indexed.</p>}
        </Card>
        <Card className="p-5">
          <h2 className="mb-2 font-semibold">Notifications</h2>
          {!notes ? <Skeleton className="h-16 w-full" /> : notes.notifications.length ? notes.notifications.map((n) => (
            <div key={n.id} className="border-b border-line py-2.5 last:border-0">
              <div className="flex justify-between gap-2 text-sm"><b className="font-medium">{n.title}</b><span className="shrink-0 text-xs text-muted">{ago(n.created_at, now)}</span></div>
              <p className="mt-0.5 whitespace-pre-line text-xs text-muted">{n.body}</p>
            </div>
          )) : <p className="py-6 text-center text-sm text-muted">No notifications yet.</p>}
        </Card>
      </div>
    </>
  );
}

function Tile({ label, value, sub }: { label: string; value: string | null; sub?: string }) {
  return (
    <Card className="p-4">
      {value == null ? <Skeleton className="h-7 w-20" /> : <div className="text-2xl font-semibold tabular-nums">{value}</div>}
      <div className="mt-0.5 text-[13px] text-muted">{label}{sub ? <span className="text-faint"> · {sub}</span> : null}</div>
    </Card>
  );
}
