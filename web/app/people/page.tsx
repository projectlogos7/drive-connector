"use client";
import { UserPlus } from "lucide-react";
import { useState } from "react";
import useSWR from "swr";
import { Button, Card, ErrorNote, PageHead, Skeleton, useConfirm, useToast } from "@/components/ui";
import { del, post } from "@/lib/api";

interface Member { id: string; email: string; name: string | null; role: string; deactivated_at: string | null }

export default function PeoplePage() {
  const toast = useToast();
  const { confirm, node } = useConfirm();
  const { data, error, mutate } = useSWR<{ members: Member[] }>("/api/members");
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  async function invite(e: React.FormEvent) {
    e.preventDefault();
    if (!email.trim()) return;
    setBusy(true); setMsg(null);
    try {
      const r = await post<{ member: Member; invite_sent?: boolean; dev_link?: string }>("/api/members", { email, name: name || null, send_invite: true });
      setMsg((r.invite_sent ? `Invite sent to ${r.member.email}.` : `Added ${r.member.email}.`) + (r.dev_link ? ` Dev link: ${r.dev_link}` : ""));
      setEmail(""); setName(""); mutate();
    } catch (err) { toast((err as Error).message, "err"); } finally { setBusy(false); }
  }
  async function remove(m: Member) {
    if (!await confirm("Remove this person’s access?", `${m.email} won’t be able to sign in any more.`, "Remove")) return;
    try { await del(`/api/members/${m.id}`); mutate(); } catch (e) { toast((e as Error).message, "err"); }
  }

  return (
    <>
      {node}
      <PageHead title="People" sub="Invite people to this account (shared index). For a totally separate tenant, run nous-drive create-account on the server." />
      <Card className="p-5">
        <form onSubmit={invite} className="flex flex-wrap gap-2">
          <input type="email" required value={email} onChange={(e) => setEmail(e.target.value)} placeholder="colleague@example.org" className="min-w-[200px] flex-[2] rounded-lg border border-line bg-panel px-3 py-2 text-sm" />
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Name (optional)" className="min-w-[140px] flex-1 rounded-lg border border-line bg-panel px-3 py-2 text-sm" />
          <Button type="submit" loading={busy} icon={<UserPlus className="h-4 w-4" />}>Invite</Button>
        </form>
        {msg && <p className="mt-2 break-words text-sm text-muted">{msg}</p>}
        <div className="mt-4">
          {error ? <ErrorNote error={error} retry={() => mutate()} /> : !data ? <Skeleton className="h-20 w-full" /> : data.members.length ? data.members.map((m) => (
            <div key={m.id} className="flex flex-wrap items-center justify-between gap-2 border-b border-line py-2.5 last:border-0">
              <div><div className="text-sm font-medium">{m.email}</div><div className="text-xs text-muted">{m.name || "—"} · {m.role}{m.deactivated_at ? " · removed" : ""}</div></div>
              {!m.deactivated_at && <Button size="sm" variant="danger" onClick={() => remove(m)}>Remove</Button>}
            </div>
          )) : <p className="py-6 text-center text-sm text-muted">No people yet.</p>}
        </div>
      </Card>
    </>
  );
}
