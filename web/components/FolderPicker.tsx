"use client";
import { ChevronRight, Folder, Loader2 } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Button, Dialog, cx } from "./ui";

type TreeNode = { name: string; kind: string; id: string; folders?: number; children?: TreeNode[] };

function Row({ source, path, node, depth, selected, onSelect }: {
  source: string; path: string; node: TreeNode; depth: number; selected: string | null; onSelect: (p: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [kids, setKids] = useState<TreeNode[] | null>(null);
  const [loading, setLoading] = useState(false);
  async function toggle() {
    const next = !open; setOpen(next);
    if (next && kids === null) {
      setLoading(true);
      try {
        const r = await api<{ tree: TreeNode }>(`/api/tree?source=${encodeURIComponent(source)}&path=${encodeURIComponent(path)}&depth=1`);
        setKids((r.tree.children || []).filter((c) => c.kind === "folder"));
      } finally { setLoading(false); }
    }
  }
  return (
    <div>
      <div className={cx("flex items-center gap-1 rounded-md px-1 py-1", selected === path ? "bg-accent-soft" : "hover:bg-bg")} style={{ paddingLeft: depth * 18 + 4 }}>
        <button type="button" onClick={toggle} aria-label="Expand" className={cx("grid h-6 w-6 place-items-center rounded text-muted", node.folders === 0 && "invisible")}>
          {loading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <ChevronRight className={cx("h-3.5 w-3.5 transition-transform", open && "rotate-90")} />}
        </button>
        <button type="button" onClick={() => onSelect(path)} className="flex min-w-0 flex-1 items-center gap-2 text-left text-sm">
          <Folder className="h-4 w-4 shrink-0 text-muted" /><span className="truncate">{node.name}</span>
        </button>
      </div>
      {open && kids && kids.map((k) => (
        <Row key={k.id} source={source} path={`${path === "/" ? "" : path}/${k.name}`} node={k} depth={depth + 1} selected={selected} onSelect={onSelect} />
      ))}
    </div>
  );
}

export function FolderPicker({ open, source, sourceName, value, onClose, onPick }: {
  open: boolean; source: string; sourceName: string; value: string | null; onClose: () => void; onPick: (path: string | null) => void;
}) {
  const [top, setTop] = useState<TreeNode[] | null>(null);
  const [sel, setSel] = useState<string | null>(value);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    if (!open) return;
    setSel(value); setTop(null); setErr(null);
    api<{ tree: TreeNode }>(`/api/tree?source=${encodeURIComponent(source)}&path=/&depth=1`)
      .then((r) => setTop((r.tree.children || []).filter((c) => c.kind === "folder"))).catch((e) => setErr(e.message));
  }, [open, source, value]);
  return (
    <Dialog open={open} onClose={onClose} title="Search only inside a folder" sub={`In ${sourceName}. Everything below the folder is included.`}
      footer={<><span className="truncate font-mono text-[13px] text-muted">{sel || "Whole source"}</span>
        <div className="flex gap-2"><Button variant="ghost" onClick={() => { onPick(null); onClose(); }}>Whole source</Button>
          <Button disabled={!sel} onClick={() => { onPick(sel); onClose(); }}>Use this folder</Button></div></>}>
      <div className="max-h-80 overflow-auto rounded-lg border border-line p-1">
        {err ? <p className="p-3 text-sm text-bad">{err}</p> : top === null ? <div className="flex items-center gap-2 p-3 text-sm text-muted"><Loader2 className="h-4 w-4 animate-spin" />Loading folders…</div>
          : top.length ? top.map((n) => <Row key={n.id} source={source} path={`/${n.name}`} node={n} depth={0} selected={sel} onSelect={setSel} />)
          : <p className="p-3 text-sm text-muted">No folders in this source yet.</p>}
      </div>
    </Dialog>
  );
}
