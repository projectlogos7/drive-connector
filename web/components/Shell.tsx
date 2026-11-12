"use client";
import { Activity, HardDrive, KeyRound, LayoutGrid, LogOut, Monitor, Search, Users } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import useSWR, { SWRConfig } from "swr";
import { fetcher, getKey, onUnauthorized, setKey } from "@/lib/api";
import { LiveProvider, useLive } from "@/lib/live";
import type { Source } from "@/lib/types";
import { ToastProvider, cx, useToast } from "./ui";

const NAV = [
  { href: "/", label: "Overview", icon: LayoutGrid },
  { href: "/search/", label: "Search", icon: Search },
  { href: "/drive/", label: "Google Drive", icon: HardDrive, count: "gdrive" },
  { href: "/computers/", label: "Computers", icon: Monitor, count: "local" },
  { href: "/activity/", label: "Activity", icon: Activity },
  { href: "/claude/", label: "MCP", icon: KeyRound },
  { href: "/people/", label: "People", icon: Users },
] as const;

export function Shell({ children }: { children: React.ReactNode }) {
  const path = usePathname();
  const router = useRouter();
  const [ready, setReady] = useState(false);
  const [authed, setAuthed] = useState(false);

  useEffect(() => {
    // Magic-link sign-in lands with the key in the URL hash (never sent to the server again).
    const h = decodeURIComponent(window.location.hash.slice(1));
    if (h.startsWith("session=")) { setKey(h.slice(8)); history.replaceState(null, "", window.location.pathname); }
    const has = !!getKey();
    setAuthed(has);
    setReady(true);
    if (!has && path !== "/login/") router.replace("/login/");
    return onUnauthorized(() => { setKey(null); setAuthed(false); router.replace("/login/"); });
  }, [path, router]);

  if (path === "/login/") return <ToastProvider>{children}</ToastProvider>;
  if (!ready || !authed) return <div className="grid min-h-screen place-items-center text-sm text-muted">Loading…</div>;

  return (
    <SWRConfig value={{ fetcher, revalidateOnFocus: true, keepPreviousData: true, errorRetryCount: 3 }}>
      <ToastProvider>
        <LiveBridge>
          <div className="min-h-screen md:grid md:grid-cols-[236px_1fr]">
            <Sidebar />
            <main className="mx-auto w-full max-w-5xl px-4 pb-20 pt-6 md:px-8 md:pt-8">{children}</main>
          </div>
        </LiveBridge>
      </ToastProvider>
    </SWRConfig>
  );
}

function LiveBridge({ children }: { children: React.ReactNode }) {
  const toast = useToast();
  return <LiveProvider onNotification={(n) => toast(n.title, n.kind === "alert" ? "err" : "info")}>{children}</LiveProvider>;
}

function Sidebar() {
  const path = usePathname();
  const router = useRouter();
  const live = useLive();
  const { data } = useSWR<{ sources: Source[] }>("/api/sources");
  const { data: me } = useSWR<{ account: { name: string } }>("/api/me");
  const counts = { gdrive: 0, local: 0 } as Record<string, number>;
  data?.sources.forEach((s) => counts[s.type]++);
  const busy = Object.values(live.progress).filter((p) => p.busy).length;

  return (
    <aside className="sticky top-0 z-30 flex flex-col gap-1 border-b border-line bg-bg/90 px-3 py-3 backdrop-blur md:h-screen md:border-b-0 md:border-r md:px-3.5 md:py-5">
      <div className="flex items-center justify-between gap-2 px-2 md:pb-5">
        <div className="flex items-center gap-2.5 font-semibold"><div className="grid h-7 w-7 place-items-center rounded-lg bg-accent text-sm text-accent-ink">N</div>Nous Drive</div>
        <LiveDot connected={live.connected} busy={busy} className="md:hidden" />
      </div>
      <nav className="-mx-1 flex gap-0.5 overflow-x-auto md:mx-0 md:flex-col">
        {NAV.map((n) => {
          const on = n.href === "/" ? path === "/" : path.startsWith(n.href);
          const Icon = n.icon;
          return (
            <Link key={n.href} href={n.href}
              className={cx("flex shrink-0 items-center gap-2.5 rounded-lg px-2.5 py-2 text-sm",
                on ? "bg-panel font-semibold text-ink shadow-card" : "text-muted hover:bg-panel hover:text-ink")}>
              <Icon className="h-[18px] w-[18px]" />{n.label}
              {"count" in n && counts[n.count] ? <span className="ml-auto hidden text-xs text-faint md:inline">{counts[n.count]}</span> : null}
            </Link>
          );
        })}
      </nav>
      <div className="mt-auto hidden px-2.5 pt-4 text-[13px] md:block">
        <LiveDot connected={live.connected} busy={busy} />
        <div className="mt-3 truncate text-muted">{me?.account.name}</div>
        <button className="mt-1 inline-flex items-center gap-1.5 text-muted hover:text-ink"
          onClick={() => { setKey(null); router.replace("/login/"); }}><LogOut className="h-3.5 w-3.5" />Sign out</button>
      </div>
    </aside>
  );
}

function LiveDot({ connected, busy, className }: { connected: boolean; busy: number; className?: string }) {
  return (
    <div className={cx("flex items-center gap-2 text-xs text-muted", className)} title={connected ? "Receiving live updates" : "Reconnecting to live updates"}>
      <span className="relative flex h-2 w-2">
        {connected && <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-ok opacity-60" />}
        <span className={cx("relative inline-flex h-2 w-2 rounded-full", connected ? "bg-ok" : "bg-warn")} />
      </span>
      {connected ? (busy ? `Live · ${busy} working` : "Live") : "Reconnecting…"}
    </div>
  );
}
