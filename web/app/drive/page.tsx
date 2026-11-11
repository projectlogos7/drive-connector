"use client";
import { HardDrive, Plus, SlidersHorizontal } from "lucide-react";
import { useEffect, useState } from "react";
import useSWR from "swr";
import { SelectionDialog, connectDrive } from "@/components/drive";
import { SourceCard } from "@/components/SourceCard";
import { Button, Card, CardSkeleton, Empty, ErrorNote, PageHead, useToast } from "@/components/ui";
import { ago, useNow } from "@/lib/format";
import type { Connection, Root, Source, SourceHealth } from "@/lib/types";

export default function DrivePage() {
  const toast = useToast();
  const now = useNow(30000);
  const { data: conns, error, mutate: reload } = useSWR<{ connections: Connection[] }>("/api/connections");
  const { data: srcs } = useSWR<{ sources: Source[] }>("/api/sources");
  const { data: status } = useSWR<{ sources: SourceHealth[] }>("/api/status", { refreshInterval: 30000 });
  const [dialog, setDialog] = useState<{ conn: string; email?: string | null; just?: boolean } | null>(null);
  const [connecting, setConnecting] = useState(false);
  const health = Object.fromEntries((status?.sources || []).map((s) => [s.source_id, s]));
  const active = conns?.connections.filter((c) => !c.revoked_at) || [];

  // Back from Google sign-in: /app/drive/#connected=<id>  or  #error=<message>
  useEffect(() => {
    const h = decodeURIComponent(window.location.hash.slice(1));
    if (h.startsWith("connected=")) {
      const id = h.slice(10);
      toast("Google account connected — indexing your whole My Drive now.", "ok");
      setDialog({ conn: id, just: true });
      history.replaceState(null, "", window.location.pathname);
    } else if (h.startsWith("error=")) {
      toast(`Google sign-in failed: ${h.slice(6)}`, "err");
      history.replaceState(null, "", window.location.pathname);
    }
  }, [toast]);

  async function connect() { setConnecting(true); try { await connectDrive(); } catch (e) { toast((e as Error).message, "err"); setConnecting(false); } }

  return (
    <>
      <PageHead title="Google Drive" sub="Connected Google accounts and what’s watched in each."
        actions={<Button loading={connecting} onClick={connect} icon={<Plus className="h-4 w-4" />}>Connect Google account</Button>} />
      {dialog && <SelectionDialog open conn={dialog.conn} email={dialog.email ?? active.find((c) => c.id === dialog.conn)?.google_email}
        justConnected={dialog.just} onClose={() => setDialog(null)} />}
      {error ? <ErrorNote error={error} retry={() => reload()} /> : !conns || !srcs ? <div className="space-y-4"><CardSkeleton /><CardSkeleton rows={1} /></div>
        : active.length === 0 ? (
          <Empty icon={<HardDrive className="h-5 w-5" />} title="Connect a Google account"
            action={<Button loading={connecting} onClick={connect}>Connect Google account</Button>}>
            Sign in with Google (read-only — nothing in Drive is ever changed). Your whole My Drive is indexed straight away,
            including searchable text from documents; you can narrow it to specific folders or add shared drives.
          </Empty>
        ) : active.map((c) => <Account key={c.id} c={c} sources={srcs.sources} health={health} now={now}
          onChoose={() => setDialog({ conn: c.id, email: c.google_email })} />)}
    </>
  );
}

function Account({ c, sources, health, now, onChoose }: { c: Connection; sources: Source[]; health: Record<string, SourceHealth>; now: number; onChoose: () => void }) {
  const { data: sel } = useSWR<{ roots: Root[] }>(`/api/connections/${c.id}/selection`);
  const ids = new Set((sel?.roots || []).map((r) => r.source_id));
  const mine = sources.filter((s) => ids.has(s.id));
  const content = c.scopes.some((s) => s.endsWith("drive.readonly"));
  return (
    <section className="mb-8">
      <Card className="mb-3 flex flex-wrap items-center justify-between gap-3 p-4">
        <div>
          <div className="font-semibold">{c.google_email || "Google account"}</div>
          <div className="text-[13px] text-muted">{content ? "Names, folders and document text (read-only)" : "Names and folders only (read-only)"} · connected {ago(c.created_at, now)}</div>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button onClick={onChoose} icon={<SlidersHorizontal className="h-4 w-4" />}>Choose what’s watched</Button>
          {!content && <Button variant="ghost" onClick={() => connectDrive(true, c.id)}>Allow document text</Button>}
        </div>
      </Card>
      <div className="space-y-3">
        {!sel ? <CardSkeleton rows={1} /> : mine.length ? mine.map((s) => <SourceCard key={s.id} source={s} health={health[s.id]} />)
          : <Card className="p-5 text-sm text-muted">Nothing watched for this account yet. <button className="text-accent hover:underline" onClick={onChoose}>Choose folders</button></Card>}
      </div>
    </section>
  );
}
