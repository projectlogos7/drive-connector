"use client";
import { Monitor, Pencil, Plus, Unplug } from "lucide-react";
import { useState } from "react";
import useSWR from "swr";
import { SourceCard } from "@/components/SourceCard";
import { Button, Card, CardSkeleton, CopyBox, Dialog, Empty, ErrorNote, PageHead, cx, useConfirm, useToast } from "@/components/ui";
import { del, patch, post } from "@/lib/api";
import { ago, useNow } from "@/lib/format";
import type { Device, Source, SourceHealth } from "@/lib/types";

export default function Computers() {
  const now = useNow(15000);
  const toast = useToast();
  const { confirm, node } = useConfirm();
  const { data: devs, error, mutate: reload } = useSWR<{ devices: Device[] }>("/api/devices", { refreshInterval: 15000 });
  const { data: srcs } = useSWR<{ sources: Source[] }>("/api/sources");
  const { data: status } = useSWR<{ sources: SourceHealth[] }>("/api/status", { refreshInterval: 30000 });
  const [pairOpen, setPairOpen] = useState(false);
  const [rename, setRename] = useState<Device | null>(null);
  const health = Object.fromEntries((status?.sources || []).map((s) => [s.source_id, s]));
  const devices = devs?.devices.filter((d) => !d.revoked_at) || [];
  const locals = srcs?.sources.filter((s) => s.type === "local") || [];

  async function disconnect(d: Device) {
    if (!await confirm("Disconnect this computer?", `“${d.name}” loses access immediately. Its folders stay in the index until you remove them.`, "Disconnect")) return;
    try { await del(`/api/devices/${d.id}`); toast("Disconnected", "ok"); reload(); } catch (e) { toast((e as Error).message, "err"); }
  }

  return (
    <>
      {node}
      <PageHead title="Computers" sub="Local folders and external drives, watched by the Nous Sync app on each computer."
        actions={<Button onClick={() => setPairOpen(true)} icon={<Plus className="h-4 w-4" />}>Add a computer</Button>} />
      <PairDialog open={pairOpen} onClose={() => { setPairOpen(false); reload(); }} />
      {rename && <RenameDialog d={rename} onClose={() => { setRename(null); reload(); }} />}
      {error ? <ErrorNote error={error} retry={() => reload()} /> : !devs || !srcs ? <div className="space-y-4"><CardSkeleton /></div>
        : devices.length === 0 ? (
          <Empty icon={<Monitor className="h-5 w-5" />} title="Add a computer" action={<Button onClick={() => setPairOpen(true)}>Add a computer</Button>}>
            Install Nous Sync on a Mac, Windows or Linux computer to index local folders and external drives.
            Names, sizes and dates are sent; document text only for files chosen for content search.
          </Empty>
        ) : devices.map((d) => {
          const mine = locals.filter((s) => s.device === d.name);
          return (
            <section key={d.id} className="mb-8">
              <Card className="mb-3 flex flex-wrap items-center justify-between gap-3 p-4">
                <div className="flex items-center gap-3">
                  <div className="grid h-9 w-9 place-items-center rounded-lg bg-accent-soft text-accent"><Monitor className="h-[18px] w-[18px]" /></div>
                  <div>
                    <div className="flex items-center gap-2 font-semibold">{d.name}
                      <span className={cx("inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-semibold", d.online ? "bg-ok-soft text-ok" : "bg-bg text-muted")}>
                        <span className="h-1.5 w-1.5 rounded-full bg-current" />{d.online ? "Online" : "Offline"}</span></div>
                    <div className="text-[13px] text-muted">{d.online ? "Connected now" : `Last seen ${ago(d.last_seen_at, now)}`}{d.os ? ` · ${d.os}` : ""}{d.agent_version ? ` · Nous Sync ${d.agent_version}` : ""}</div>
                  </div>
                </div>
                <div className="flex gap-2">
                  <Button size="sm" variant="ghost" icon={<Pencil className="h-3.5 w-3.5" />} onClick={() => setRename(d)}>Rename</Button>
                  <Button size="sm" variant="danger" icon={<Unplug className="h-3.5 w-3.5" />} onClick={() => disconnect(d)}>Disconnect</Button>
                </div>
              </Card>
              <div className="space-y-3">
                {mine.length ? mine.map((s) => <SourceCard key={s.id} source={s} health={health[s.id]} />)
                  : <Card className="p-5 text-sm text-muted">No folders yet. On this computer run <code className="rounded bg-bg px-1.5 py-0.5 font-mono">nous-sync add ~/Footage</code></Card>}
              </div>
            </section>
          );
        })}
    </>
  );
}

function PairDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const toast = useToast();
  const [name, setName] = useState("");
  const [code, setCode] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const server = typeof window !== "undefined" ? window.location.origin : "";
  async function create() {
    setBusy(true);
    try { setCode((await post<{ code: string }>("/api/devices/pairing-codes", { name: name || null })).code); }
    catch (e) { toast((e as Error).message, "err"); } finally { setBusy(false); }
  }
  const close = () => { setCode(null); setName(""); onClose(); };
  const nameArg = name ? ` --name "${name}"` : "";
  return (
    <Dialog open={open} onClose={close} title="Add a computer" sub="Name it, then run one command on that computer."
      footer={<><span className="text-[13px] text-muted">{code ? "The computer appears here as soon as it connects." : ""}</span>
        <div className="flex gap-2"><Button variant="ghost" onClick={close}>{code ? "Done" : "Cancel"}</Button>{!code && <Button loading={busy} onClick={create}>Create pairing code</Button>}</div></>}>
      {!code ? (
        <input autoFocus value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Nous MacBook Pro"
          onKeyDown={(e) => e.key === "Enter" && create()} className="w-full rounded-lg border border-line bg-panel px-3 py-2.5 text-sm" />
      ) : (
        <div className="pb-2">
          <p className="text-sm">Pairing code <b className="font-mono text-base">{code}</b> <span className="text-muted">— valid for 15 minutes, single use.</span></p>
          <CopyBox label="Mac / Linux — install and pair" text={`agent/installers/install.sh --code ${code} --server ${server}${nameArg}`} />
          <CopyBox label="Windows (PowerShell)" text={`agent\\installers\\install.ps1 -Code ${code} -Server ${server}${name ? ` -Name "${name}"` : ""}`} />
          <CopyBox label="Already installed? Pair, then add folders" text={`nous-sync pair ${code} --server ${server}${nameArg}\nnous-sync add ~/Footage`} />
        </div>
      )}
    </Dialog>
  );
}

function RenameDialog({ d, onClose }: { d: Device; onClose: () => void }) {
  const toast = useToast();
  const [name, setName] = useState(d.name);
  const [busy, setBusy] = useState(false);
  async function save() {
    setBusy(true);
    try { await patch(`/api/devices/${d.id}`, { name }); toast("Renamed", "ok"); onClose(); } catch (e) { toast((e as Error).message, "err"); } finally { setBusy(false); }
  }
  return (
    <Dialog open onClose={onClose} title="Rename computer"
      footer={<><span /><div className="flex gap-2"><Button variant="ghost" onClick={onClose}>Cancel</Button><Button loading={busy} disabled={!name.trim()} onClick={save}>Save</Button></div></>}>
      <input autoFocus value={name} onChange={(e) => setName(e.target.value)} onKeyDown={(e) => e.key === "Enter" && save()}
        className="w-full rounded-lg border border-line bg-panel px-3 py-2.5 text-sm" />
    </Dialog>
  );
}
