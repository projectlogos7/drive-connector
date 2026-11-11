"use client";
import { KeyRound, Trash2 } from "lucide-react";
import { useState } from "react";
import useSWR from "swr";
import { Button, Card, CopyBox, ErrorNote, PageHead, Skeleton, useConfirm, useToast } from "@/components/ui";
import { del, post } from "@/lib/api";
import { ago, useNow } from "@/lib/format";

interface Key { id: string; kind: string; name: string; key_prefix: string; created_at: string; last_used_at: string | null; revoked_at: string | null }

export default function ClaudePage() {
  const toast = useToast();
  const now = useNow(30000);
  const { confirm, node } = useConfirm();
  const { data, error, mutate } = useSWR<{ keys: Key[] }>("/api/keys");
  const [name, setName] = useState("");
  const [created, setCreated] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const mcpUrl = "https://<your-mcp-server>/mcp";

  async function create() {
    setBusy(true);
    try { const r = await post<{ key: string }>("/api/keys", { name: name.trim() || "Claude session", kind: "session" }); setCreated(r.key); setName(""); mutate(); }
    catch (e) { toast((e as Error).message, "err"); } finally { setBusy(false); }
  }
  async function revoke(k: Key) {
    if (!await confirm("Revoke this key?", `Anything using “${k.name}” loses access immediately.`, "Revoke")) return;
    try { await del(`/api/keys/${k.id}`); mutate(); } catch (e) { toast((e as Error).message, "err"); }
  }

  return (
    <>
      {node}
      <PageHead title="Claude access" sub="Let Claude (or any MCP client) search and browse the index." />
      <Card className="mb-4 p-5">
        <h2 className="font-semibold">Create a key</h2>
        <p className="mt-1 text-sm text-muted">One key per Claude app or session, so each keeps its own “what’s new since I last asked”.</p>
        <div className="mt-3 flex flex-wrap gap-2">
          <input value={name} onChange={(e) => setName(e.target.value)} onKeyDown={(e) => e.key === "Enter" && create()} placeholder="e.g. Claude Desktop – MacBook"
            className="min-w-[220px] flex-1 rounded-lg border border-line bg-panel px-3 py-2 text-sm" />
          <Button loading={busy} onClick={create} icon={<KeyRound className="h-4 w-4" />}>Create key</Button>
        </div>
        {created && (
          <div className="mt-4 animate-pop">
            <div className="rounded-lg bg-accent-soft px-3 py-2 text-sm">Copy this key now — it won’t be shown again.</div>
            <CopyBox text={created} />
            <CopyBox label="Claude Code" text={`claude mcp add --transport http nous-drive ${mcpUrl} \\\n  --header "Authorization: Bearer ${created}"`} />
            <CopyBox label="Claude Desktop (claude_desktop_config.json, needs Node.js)" text={JSON.stringify({ mcpServers: { "nous-drive": { command: "npx", args: ["-y", "mcp-remote", mcpUrl, "--header", `Authorization: Bearer ${created}`] } } }, null, 2)} />
            <p className="mt-2 text-xs text-muted">Replace the MCP address with where <code className="font-mono">nous-drive mcp --http</code> runs. Full guide: docs/GETTING_STARTED.md.</p>
          </div>
        )}
      </Card>
      <Card className="p-5">
        <h2 className="mb-2 font-semibold">Keys</h2>
        {error ? <ErrorNote error={error} retry={() => mutate()} /> : !data ? <Skeleton className="h-20 w-full" /> : (
          <div className="overflow-x-auto"><table className="w-full text-sm">
            <thead><tr className="border-b border-line text-left text-xs text-muted"><th className="py-2 font-medium">Name</th><th className="font-medium">Type</th><th className="font-medium">Key</th><th className="font-medium">Last used</th><th /></tr></thead>
            <tbody>{data.keys.filter((k) => !k.revoked_at && k.kind !== "device").map((k) => (
              <tr key={k.id} className="border-b border-line last:border-0">
                <td className="py-2.5">{k.name}</td><td>{k.kind === "session" ? "Claude / MCP" : "Admin"}</td>
                <td><code className="font-mono text-xs">{k.key_prefix}…</code></td><td className="text-muted">{ago(k.last_used_at, now)}</td>
                <td className="text-right"><Button size="sm" variant="danger" icon={<Trash2 className="h-3.5 w-3.5" />} onClick={() => revoke(k)}>Revoke</Button></td>
              </tr>))}</tbody>
          </table></div>
        )}
      </Card>
    </>
  );
}
