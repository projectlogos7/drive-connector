"use client";
import { CheckCircle2, Circle, KeyRound, PlugZap, Trash2, TriangleAlert } from "lucide-react";
import { useMemo, useState } from "react";
import useSWR from "swr";
import { Button, Card, CopyBox, ErrorNote, PageHead, Skeleton, cx, useConfirm, useToast } from "@/components/ui";
import { del, post } from "@/lib/api";
import { ago, useNow } from "@/lib/format";

interface Key {
  id: string;
  kind: string;
  name: string;
  key_prefix: string;
  created_at: string;
  last_used_at: string | null;
  revoked_at: string | null;
  connection?: "connected" | "idle" | "never" | "revoked";
}

interface McpStatus {
  mcp_url: string;
  server: "up" | "down" | "unknown";
  connected: boolean;
  keys: Key[];
}

const CONN: Record<string, { label: string; className: string; Icon: typeof CheckCircle2 }> = {
  connected: { label: "Connected", className: "bg-ok-soft text-ok", Icon: CheckCircle2 },
  idle: { label: "Idle", className: "bg-warn-soft text-warn", Icon: Circle },
  never: { label: "Not connected", className: "bg-bg text-muted", Icon: Circle },
  revoked: { label: "Revoked", className: "bg-bad-soft text-bad", Icon: TriangleAlert },
};

export default function ClaudePage() {
  const toast = useToast();
  const now = useNow(30000);
  const { confirm, node } = useConfirm();
  const { data, error, mutate } = useSWR<McpStatus>("/api/mcp", { refreshInterval: 15000 });
  const [name, setName] = useState("");
  const [created, setCreated] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [tab, setTab] = useState<"code" | "desktop" | "stdio">("code");

  const mcpUrl = data?.mcp_url || "https://<your-mcp-server>/mcp";
  const sessionKeys = useMemo(
    () => (data?.keys || []).filter((k) => !k.revoked_at && k.kind !== "device"),
    [data],
  );

  async function create() {
    setBusy(true);
    try {
      const r = await post<{ key: string }>("/api/keys", { name: name.trim() || "Claude session", kind: "session" });
      setCreated(r.key);
      setName("");
      mutate();
    } catch (e) {
      toast((e as Error).message, "err");
    } finally {
      setBusy(false);
    }
  }

  async function revoke(k: Key) {
    if (!await confirm("Revoke this key?", `Anything using “${k.name}” loses access immediately.`, "Revoke")) return;
    try {
      await del(`/api/keys/${k.id}`);
      mutate();
    } catch (e) {
      toast((e as Error).message, "err");
    }
  }

  const codeCmd = created
    ? `claude mcp add --transport http nous-drive ${mcpUrl} \\\n  --header "Authorization: Bearer ${created}"`
    : `claude mcp add --transport http nous-drive ${mcpUrl} \\\n  --header "Authorization: Bearer nd_sess_…"`;

  const desktopRemote = JSON.stringify({
    mcpServers: {
      "nous-drive": {
        command: "npx",
        args: ["-y", "mcp-remote", mcpUrl, "--header", `Authorization: Bearer ${created || "nd_sess_…"}`],
      },
    },
  }, null, 2);

  const desktopStdio = JSON.stringify({
    mcpServers: {
      "nous-drive": {
        command: "nous-drive",
        args: ["mcp"],
        env: {
          NOUS_DRIVE_DATABASE_URL: "postgresql://…",
          NOUS_DRIVE_API_KEY: created || "nd_sess_…",
        },
      },
    },
  }, null, 2);

  return (
    <>
      {node}
      <PageHead
        title="MCP"
        sub="Connect Claude (or any MCP client) to search and browse the index."
      />

      {/* Connection status */}
      <Card className="mb-4 p-5">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="flex items-start gap-3">
            <div className={cx(
              "mt-0.5 grid h-9 w-9 place-items-center rounded-lg",
              data?.connected ? "bg-ok-soft text-ok" : data?.server === "up" ? "bg-warn-soft text-warn" : "bg-bg text-muted",
            )}>
              <PlugZap className="h-4 w-4" />
            </div>
            <div>
              <h2 className="font-semibold">
                {!data ? "Checking MCP…"
                  : data.connected ? "MCP connected"
                    : data.server === "up" ? "MCP server is up — waiting for a client"
                      : data.server === "down" ? "MCP server not reachable"
                        : "MCP not connected yet"}
              </h2>
              <p className="mt-1 text-sm text-muted">
                {!data ? "Loading endpoint and key activity."
                  : data.connected
                    ? "At least one key was used in the last 15 minutes."
                    : data.server === "up"
                      ? "Create a key below and add it in Claude. Status flips to Connected when Claude first calls a tool."
                      : data.server === "down"
                        ? "The HTTP MCP endpoint did not respond. Run nous-drive mcp --http, or use local stdio (no remote server needed)."
                        : "Create a key and add the server in Claude Code or Desktop."}
              </p>
            </div>
          </div>
          <div className="flex flex-wrap gap-2 text-xs">
            <span className={cx(
              "inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 font-semibold",
              data?.server === "up" ? "bg-ok-soft text-ok" : data?.server === "down" ? "bg-bad-soft text-bad" : "bg-bg text-muted",
            )}>
              <span className="h-1.5 w-1.5 rounded-full bg-current" />
              Server {data?.server === "up" ? "up" : data?.server === "down" ? "down" : "…"}
            </span>
            <span className={cx(
              "inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 font-semibold",
              data?.connected ? "bg-ok-soft text-ok" : "bg-bg text-muted",
            )}>
              <span className="h-1.5 w-1.5 rounded-full bg-current" />
              {data?.connected ? "Client connected" : "No recent client"}
            </span>
          </div>
        </div>
        {data && (
          <div className="mt-4">
            <div className="mb-1 text-xs font-semibold uppercase tracking-wide text-faint">MCP endpoint</div>
            <CopyBox text={mcpUrl} />
          </div>
        )}
      </Card>

      {/* Step 1: create key */}
      <Card className="mb-4 p-5">
        <div className="mb-1 text-xs font-semibold uppercase tracking-wide text-faint">Step 1</div>
        <h2 className="font-semibold">Create a key</h2>
        <p className="mt-1 text-sm text-muted">
          One key per Claude app or session, so each keeps its own “what’s new since I last asked.”
        </p>
        <div className="mt-3 flex flex-wrap gap-2">
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && create()}
            placeholder="e.g. Claude Desktop – MacBook"
            className="min-w-[220px] flex-1 rounded-lg border border-line bg-panel px-3 py-2 text-sm"
          />
          <Button loading={busy} onClick={create} icon={<KeyRound className="h-4 w-4" />}>Create key</Button>
        </div>
        {created && (
          <div className="mt-4 animate-pop">
            <div className="rounded-lg bg-accent-soft px-3 py-2 text-sm">Copy this key now — it won’t be shown again.</div>
            <CopyBox text={created} />
          </div>
        )}
      </Card>

      {/* Step 2: add to client */}
      <Card className="mb-4 p-5">
        <div className="mb-1 text-xs font-semibold uppercase tracking-wide text-faint">Step 2</div>
        <h2 className="font-semibold">Add the MCP server in Claude</h2>
        <p className="mt-1 text-sm text-muted">
          Pick how you want to connect. Remote HTTP needs the MCP process running; local stdio talks to the database from your machine (no MCP container).
        </p>
        <div className="mt-3 flex flex-wrap gap-1.5">
          {([
            ["code", "Claude Code"],
            ["desktop", "Claude Desktop (remote)"],
            ["stdio", "Claude Desktop (local / cheaper)"],
          ] as const).map(([id, label]) => (
            <button
              key={id}
              type="button"
              onClick={() => setTab(id)}
              className={cx(
                "rounded-lg px-3 py-1.5 text-sm",
                tab === id ? "bg-ink text-bg" : "bg-bg text-muted hover:text-ink",
              )}
            >
              {label}
            </button>
          ))}
        </div>

        {tab === "code" && (
          <div className="mt-4">
            <p className="text-sm text-muted">In a terminal:</p>
            <CopyBox label="Claude Code" text={codeCmd} />
            <p className="mt-2 text-xs text-muted">Then ask Claude something like “What’s new in my Drive?”</p>
          </div>
        )}
        {tab === "desktop" && (
          <div className="mt-4">
            <p className="text-sm text-muted">
              Settings → Developer → Edit Config. Needs Node.js for the <code className="font-mono text-[12px]">mcp-remote</code> bridge.
            </p>
            <CopyBox label="claude_desktop_config.json" text={desktopRemote} />
            <p className="mt-2 text-xs text-muted">Restart Claude Desktop; “nous-drive” appears in the tools list.</p>
          </div>
        )}
        {tab === "stdio" && (
          <div className="mt-4">
            <p className="text-sm text-muted">
              Runs MCP on your laptop against Neon directly — no remote MCP server, so you can turn off the <code className="font-mono text-[12px]">mcp</code> container.
              Install the package locally first (<code className="font-mono text-[12px]">pip install -e .</code>).
            </p>
            <CopyBox label="claude_desktop_config.json" text={desktopStdio} />
            <p className="mt-2 text-xs text-muted">
              Use a read-only or app-role database URL when you can. Same tools as HTTP MCP; RLS still scopes every query to this account.
            </p>
          </div>
        )}
      </Card>

      {/* Keys */}
      <Card className="p-5">
        <h2 className="mb-2 font-semibold">Keys &amp; connection</h2>
        {error ? <ErrorNote error={error} retry={() => mutate()} /> : !data ? <Skeleton className="h-20 w-full" /> : (
          sessionKeys.length === 0 ? (
            <p className="py-6 text-center text-sm text-muted">No keys yet. Create one above to connect Claude.</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-line text-left text-xs text-muted">
                    <th className="py-2 font-medium">Name</th>
                    <th className="font-medium">Status</th>
                    <th className="font-medium">Key</th>
                    <th className="font-medium">Last used</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {sessionKeys.map((k) => {
                    const c = CONN[k.connection || (k.last_used_at ? "idle" : "never")];
                    const Icon = c.Icon;
                    return (
                      <tr key={k.id} className="border-b border-line last:border-0">
                        <td className="py-2.5">{k.name}</td>
                        <td>
                          <span className={cx("inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-semibold", c.className)}>
                            <Icon className="h-3 w-3" />{c.label}
                          </span>
                        </td>
                        <td><code className="font-mono text-xs">{k.key_prefix}…</code></td>
                        <td className="text-muted">{ago(k.last_used_at, now)}</td>
                        <td className="text-right">
                          <Button size="sm" variant="danger" icon={<Trash2 className="h-3.5 w-3.5" />} onClick={() => revoke(k)}>Revoke</Button>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )
        )}
      </Card>
    </>
  );
}
