"use client";
import { useEffect, useState } from "react";
import { patch } from "@/lib/api";
import type { Source } from "@/lib/types";
import { Button, Dialog, useToast } from "./ui";

const TYPES = ["added", "modified", "moved", "renamed", "deleted"];

export function AlertsDialog({ open, onClose, source }: { open: boolean; onClose: () => void; source: Source }) {
  const toast = useToast();
  const [enabled, setEnabled] = useState(true);
  const [types, setTypes] = useState<string[]>(["added"]);
  const [quiet, setQuiet] = useState("");
  const [saving, setSaving] = useState(false);
  useEffect(() => { if (open) { setEnabled(true); setTypes(["added"]); setQuiet(""); } }, [open]);

  async function save() {
    setSaving(true);
    try {
      await patch(`/api/sources/${source.id}/settings`, { notifications: { enabled, types, quiet_folders: quiet.split("\n").map((x) => x.trim()).filter(Boolean) } });
      toast("Alert settings saved", "ok"); onClose();
    } catch (e) { toast((e as Error).message, "err"); } finally { setSaving(false); }
  }
  return (
    <Dialog open={open} onClose={onClose} title={`Alerts for ${source.name}`} sub="New files are announced in batches — one message per upload, not one per file."
      footer={<><span /><div className="flex gap-2"><Button variant="ghost" onClick={onClose}>Cancel</Button><Button loading={saving} onClick={save}>Save</Button></div></>}>
      <label className="flex items-center gap-2 py-1 text-sm"><input type="checkbox" className="h-4 w-4 accent-[rgb(var(--accent))]" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />Send notifications for this source</label>
      <div className="mt-4 text-xs font-semibold uppercase tracking-wide text-faint">Tell me when files are</div>
      <div className="mt-1 grid grid-cols-2 gap-1 sm:grid-cols-3">
        {TYPES.map((t) => (
          <label key={t} className="flex items-center gap-2 py-1 text-sm capitalize">
            <input type="checkbox" className="h-4 w-4 accent-[rgb(var(--accent))]" disabled={!enabled} checked={types.includes(t)}
              onChange={(e) => setTypes((x) => (e.target.checked ? [...x, t] : x.filter((y) => y !== t)))} />{t}
          </label>
        ))}
      </div>
      <div className="mt-4 text-xs font-semibold uppercase tracking-wide text-faint">Quiet folders (one path per line)</div>
      <textarea value={quiet} onChange={(e) => setQuiet(e.target.value)} rows={3} placeholder="/Render Cache"
        className="mt-1 w-full rounded-lg border border-line bg-panel px-3 py-2 font-mono text-sm" />
    </Dialog>
  );
}
