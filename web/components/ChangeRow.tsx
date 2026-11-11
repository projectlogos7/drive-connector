"use client";
import { FileText, Folder } from "lucide-react";
import { ago, bytes } from "@/lib/format";
import type { Change } from "@/lib/types";
import { cx } from "./ui";

const TONE: Record<string, string> = {
  added: "bg-ok-soft text-ok", deleted: "bg-bad-soft text-bad", removed: "bg-bad-soft text-bad", trashed: "bg-warn-soft text-warn",
  modified: "bg-accent-soft text-accent", renamed: "bg-accent-soft text-accent", moved: "bg-accent-soft text-accent",
};

export function ChangeRow({ c, now, fresh }: { c: Change; now: number; fresh?: boolean }) {
  const from = c.from ?? c.from_path;
  return (
    <div className={cx("grid grid-cols-[auto_1fr_auto] items-start gap-3 border-b border-line py-2.5 last:border-0", fresh && "animate-flash rounded-md")}>
      <span className={cx("mt-0.5 w-[76px] rounded-md px-1.5 py-0.5 text-center text-[11px] font-semibold uppercase tracking-wide", TONE[c.type] || "bg-bg text-muted")}>{c.type}</span>
      <div className="min-w-0">
        <div className="flex min-w-0 items-center gap-1.5 text-sm">
          {c.kind === "folder" ? <Folder className="h-3.5 w-3.5 shrink-0 text-muted" /> : <FileText className="h-3.5 w-3.5 shrink-0 text-muted" />}
          <span className="truncate font-medium">{c.name || c.path}</span>
          {c.size != null && c.kind !== "folder" && <span className="shrink-0 text-xs tabular-nums text-faint">{bytes(c.size)}</span>}
        </div>
        <div className="truncate text-xs text-muted">{c.path}{from && from !== c.path ? ` · from ${from}` : ""}</div>
      </div>
      <div className="text-right text-xs text-muted"><div className="truncate">{c.source}{c.device ? ` · ${c.device}` : ""}</div><div>{ago(c.detected_at, now)}</div></div>
    </div>
  );
}
