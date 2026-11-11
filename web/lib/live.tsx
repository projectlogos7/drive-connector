"use client";
/**
 * Live data for the whole app: one streaming connection to /api/live (server-sent events read with
 * fetch, so the bearer token stays in a header). Provides per-source progress, a rolling feed of
 * changes, notifications, and throughput/ETA estimates from consecutive progress snapshots.
 */
import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import { mutate } from "swr";
import { getKey } from "./api";
import type { Change, Notification, Progress } from "./types";

interface Rate { perSec: number; etaSec: number | null }
interface LiveState {
  connected: boolean;
  lastEventAt: number | null;
  progress: Record<string, Progress>;
  changes: Change[];
  notifications: Notification[];
  rates: Record<string, Rate>;
  newChangeIds: Set<number>;
}

const LiveCtx = createContext<LiveState | null>(null);
export const useLive = () => {
  const v = useContext(LiveCtx);
  if (!v) throw new Error("LiveProvider missing");
  return v;
};

type Sample = { t: number; stage: string; done: number };

export function LiveProvider({ children, onNotification }: { children: React.ReactNode; onNotification?: (n: Notification) => void }) {
  const [state, setState] = useState<LiveState>({
    connected: false, lastEventAt: null, progress: {}, changes: [], notifications: [], rates: {}, newChangeIds: new Set(),
  });
  const samples = useRef<Record<string, Sample[]>>({});
  const prevStatus = useRef<Record<string, string>>({});
  const notifyRef = useRef(onNotification);
  notifyRef.current = onNotification;

  const handle = useCallback((event: string, data: any) => {
    const now = Date.now();
    if (event === "progress") {
      const list: Progress[] = data.sources || [];
      const rates: Record<string, Rate> = {};
      let statusChanged = false;
      for (const p of list) {
        if (prevStatus.current[p.source_id] !== undefined && prevStatus.current[p.source_id] !== p.status) statusChanged = true;
        prevStatus.current[p.source_id] = p.status;
        const active = p.stages.find((s) => s.state === "active");
        const arr = (samples.current[p.source_id] ||= []);
        if (active && active.done != null) {
          if (arr.length && arr[arr.length - 1].stage !== active.key) arr.length = 0; // new stage: restart window
          arr.push({ t: now, stage: active.key, done: active.done });
          while (arr.length > 2 && now - arr[0].t > 30000) arr.shift();
          const first = arr[0], last = arr[arr.length - 1];
          const dt = (last.t - first.t) / 1000;
          if (dt >= 2 && last.done > first.done) {
            const perSec = (last.done - first.done) / dt;
            const left = active.total != null ? Math.max(0, active.total - active.done) : null;
            rates[p.source_id] = { perSec, etaSec: left != null ? left / perSec : null };
          }
        } else {
          arr.length = 0;
        }
      }
      setState((s) => ({ ...s, lastEventAt: now, progress: Object.fromEntries(list.map((p) => [p.source_id, p])), rates }));
      if (statusChanged) { mutate("/api/sources"); mutate("/api/status"); }
    } else if (event === "changes") {
      const incoming: Change[] = data.changes || [];
      setState((s) => {
        const ids = new Set(incoming.map((c) => c.id!).filter(Boolean));
        return { ...s, lastEventAt: now, changes: [...incoming.slice().reverse(), ...s.changes].slice(0, 300), newChangeIds: ids };
      });
    } else if (event === "notification") {
      setState((s) => ({ ...s, lastEventAt: now, notifications: [data, ...s.notifications].slice(0, 50) }));
      notifyRef.current?.(data);
      mutate("/api/notifications?limit=6");
    }
  }, []);

  useEffect(() => {
    let stop = false;
    let ctrl: AbortController | null = null;
    let backoff = 1000;

    async function connect() {
      while (!stop) {
        const key = getKey();
        if (!key) { await sleep(1500); continue; }
        ctrl = new AbortController();
        try {
          const res = await fetch("/api/live", { headers: { Authorization: `Bearer ${key}` }, signal: ctrl.signal, cache: "no-store" });
          if (!res.ok || !res.body) throw new Error(`live ${res.status}`);
          setState((s) => ({ ...s, connected: true }));
          backoff = 1000;
          const reader = res.body.getReader();
          const dec = new TextDecoder();
          let buf = "";
          for (;;) {
            const { value, done } = await reader.read();
            if (done) break;
            buf += dec.decode(value, { stream: true });
            let i: number;
            while ((i = buf.indexOf("\n\n")) >= 0) {
              const chunk = buf.slice(0, i); buf = buf.slice(i + 2);
              let ev = "message", payload = "";
              for (const line of chunk.split("\n")) {
                if (line.startsWith("event:")) ev = line.slice(6).trim();
                else if (line.startsWith("data:")) payload += line.slice(5).trim();
              }
              if (payload) { try { handle(ev, JSON.parse(payload)); } catch { /* ignore bad frame */ } }
            }
          }
        } catch { /* reconnect below */ }
        setState((s) => ({ ...s, connected: false }));
        if (stop) break;
        await sleep(backoff);
        backoff = Math.min(backoff * 2, 15000);
      }
    }
    connect();
    return () => { stop = true; ctrl?.abort(); };
  }, [handle]);

  return <LiveCtx.Provider value={state}>{children}</LiveCtx.Provider>;
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
