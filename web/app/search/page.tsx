"use client";
import { ExternalLink, FileText, Folder, FolderSearch, Search as SearchIcon, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import useSWR from "swr";
import { FolderPicker } from "@/components/FolderPicker";
import { Card, ErrorNote, PageHead, Skeleton, cx } from "@/components/ui";
import { bytes } from "@/lib/format";
import type { Source } from "@/lib/types";

interface Result {
  id: string; name: string; kind: string; path: string; source: string; source_id: string; size?: number | null;
  type?: string | null; ext?: string | null; modified_at: string | null; web_link?: string; local_path?: string;
  device?: string; match: ("name" | "content")[]; snippet: string | null; summary?: string; content_status?: string;
}
interface Resp { results: Result[]; returned: number; content_search: boolean }

const TYPES: [string, string][] = [["", "Any type"], ["document", "Documents"], ["pdf", "PDFs"], ["spreadsheet", "Spreadsheets"],
  ["presentation", "Presentations"], ["text", "Text & notes"], ["video", "Video"], ["audio", "Audio"], ["image", "Images"], ["project", "Edit projects"]];
const TIMES: [string, string][] = [["", "Any time"], ["today", "Today"], ["7d", "Last 7 days"], ["30d", "Last 30 days"], ["365d", "Last year"]];
type Filters = { q: string; mode: string; source: string; folder: string; type: string; modified: string; added: string };
const EMPTY: Filters = { q: "", mode: "all", source: "", folder: "", type: "", modified: "", added: "" };

function readUrl(): Filters {
  if (typeof window === "undefined") return EMPTY;
  const p = new URLSearchParams(window.location.search);
  return { q: p.get("q") || "", mode: p.get("mode") || "all", source: p.get("source") || "", folder: p.get("folder") || "",
    type: p.get("type") || "", modified: p.get("modified") || "", added: p.get("added") || "" };
}

function Highlight({ text, q }: { text: string; q: string }) {
  const terms = q.toLowerCase().split(/\s+/).filter((t) => t.length > 1).map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  if (!terms.length) return <>{text}</>;
  const re = new RegExp(`(${terms.join("|")})`, "gi");
  return <>{text.split(re).map((part, i) => i % 2 ? <mark key={i} className="rounded bg-warn-soft px-0.5 text-ink">{part}</mark> : part)}</>;
}

export default function SearchPage() {
  const [f, setF] = useState<Filters>(EMPTY);
  const [debounced, setDebounced] = useState<Filters>(EMPTY);
  const [picker, setPicker] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const { data: srcs } = useSWR<{ sources: Source[] }>("/api/sources");

  useEffect(() => { const u = readUrl(); setF(u); setDebounced(u); input.current?.focus(); }, []);
  useEffect(() => {
    const t = setTimeout(() => setDebounced(f), f.q === debounced.q ? 0 : 300);
    const p = new URLSearchParams(Object.entries(f).filter(([k, v]) => v && !(k === "mode" && v === "all")) as [string, string][]);
    history.replaceState(null, "", `${window.location.pathname}${p.toString() ? `?${p}` : ""}`);
    return () => clearTimeout(t);
  }, [f]); // eslint-disable-line react-hooks/exhaustive-deps

  const key = useMemo(() => {
    if (debounced.q.trim().length < 2) return null;
    const p = new URLSearchParams({ q: debounced.q.trim(), mode: debounced.mode, limit: "40" });
    if (debounced.source) p.set("source", debounced.source);
    if (debounced.source && debounced.folder) p.set("folder", debounced.folder);
    if (debounced.type) p.set("type", debounced.type);
    if (debounced.modified) p.set("modified_after", debounced.modified);
    if (debounced.added) p.set("added_after", debounced.added);
    return `/api/search/all?${p}`;
  }, [debounced]);
  const { data, error, isLoading, mutate } = useSWR<Resp>(key, { keepPreviousData: true, revalidateOnFocus: false });
  const set = (patch: Partial<Filters>) => setF((x) => ({ ...x, ...patch }));
  const source = srcs?.sources.find((s) => s.id === f.source);
  const stale = data && key && isLoading;

  return (
    <>
      <PageHead title="Search" sub="Search inside documents and by file name, across Drive and your computers." />
      <Card className="p-4">
        <div className="relative">
          <SearchIcon className="pointer-events-none absolute left-3.5 top-1/2 h-5 w-5 -translate-y-1/2 text-faint" />
          <input ref={input} value={f.q} onChange={(e) => set({ q: e.target.value })} placeholder="e.g. Bengal partition Curzon, or Unmaking_Citizens"
            className="w-full rounded-xl border border-line bg-panel py-3 pl-11 pr-10 text-base" aria-label="Search" />
          {f.q && <button aria-label="Clear" onClick={() => set({ q: "" })} className="absolute right-3 top-1/2 -translate-y-1/2 rounded p-1 text-muted hover:text-ink"><X className="h-4 w-4" /></button>}
        </div>
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <div className="inline-flex rounded-lg border border-line bg-bg p-0.5 text-sm">
            {[["all", "All"], ["content", "Contents"], ["name", "Names"]].map(([v, l]) => (
              <button key={v} onClick={() => set({ mode: v })} className={cx("rounded-md px-3 py-1", f.mode === v ? "bg-panel font-semibold shadow-card" : "text-muted hover:text-ink")}>{l}</button>
            ))}
          </div>
          <Sel value={f.source} onChange={(v) => set({ source: v, folder: "" })} options={[["", "All sources"], ...(srcs?.sources.map((s) => [s.id, s.name] as [string, string]) || [])]} />
          <button disabled={!f.source} onClick={() => setPicker(true)} title={f.source ? "Search only inside a folder" : "Choose a source first"}
            className={cx("inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-sm disabled:cursor-not-allowed disabled:opacity-50",
              f.folder ? "border-accent bg-accent-soft" : "border-line bg-panel")}>
            <FolderSearch className="h-4 w-4" />{f.folder ? <span className="max-w-[200px] truncate font-mono text-[13px]">{f.folder}</span> : "In folder…"}
            {f.folder && <X className="h-3.5 w-3.5 text-muted" onClick={(e) => { e.stopPropagation(); set({ folder: "" }); }} />}
          </button>
          <Sel value={f.type} onChange={(v) => set({ type: v })} options={TYPES} />
          <Sel value={f.modified} onChange={(v) => set({ modified: v })} options={TIMES.map(([v, l]) => [v, v ? `Modified: ${l.toLowerCase()}` : "Modified: any time"])} />
          <Sel value={f.added} onChange={(v) => set({ added: v })} options={TIMES.map(([v, l]) => [v, v ? `Added: ${l.toLowerCase()}` : "Added: any time"])} />
          {(f.source || f.type || f.modified || f.added || f.mode !== "all") &&
            <button onClick={() => setF({ ...EMPTY, q: f.q })} className="text-sm text-accent hover:underline">Reset filters</button>}
        </div>
      </Card>
      {source && <FolderPicker open={picker} source={source.id} sourceName={source.name} value={f.folder || null} onClose={() => setPicker(false)} onPick={(p) => set({ folder: p || "" })} />}

      <div className={cx("mt-5 transition-opacity", stale && "opacity-60")}>
        {!key ? (
          <Card className="px-6 py-10 text-center text-sm text-muted">
            <SearchIcon className="mx-auto mb-3 h-8 w-8 text-faint" />
            Type at least two characters. <b className="text-ink">Contents</b> finds words and meaning inside documents (PDF, Word, Docs, Sheets, slides, text);
            <b className="text-ink"> Names</b> matches file and folder names and paths. Narrow it with a source, a folder, a type or a time range.
          </Card>
        ) : error ? <ErrorNote error={error} retry={() => mutate()} /> : !data ? (
          <div className="space-y-3">{[0, 1, 2, 3].map((i) => <Card key={i} className="p-4"><Skeleton className="h-4 w-56" /><Skeleton className="mt-2 h-3 w-80 max-w-full" /><Skeleton className="mt-3 h-10 w-full" /></Card>)}</div>
        ) : (
          <>
            <div className="mb-2 flex flex-wrap justify-between gap-2 text-[13px] text-muted">
              <span>{data.returned ? `${data.returned} result${data.returned === 1 ? "" : "s"}` : "No results"}{f.folder ? ` in ${f.folder}` : ""}{source ? ` · ${source.name}` : ""}</span>
              {!data.content_search && <span>Content search isn’t set up on this server — searching names only.</span>}
            </div>
            {data.results.length === 0 ? (
              <Card className="px-6 py-10 text-center text-sm text-muted">Nothing matched. Try fewer words, “All” instead of one mode, or remove a filter.
                {f.mode !== "content" ? "" : " Documents still being read won’t appear in content results until they’re processed."}</Card>
            ) : (
              <div className="space-y-2.5">{data.results.map((r) => <ResultCard key={r.id} r={r} q={debounced.q} onFolder={(folder) => setF((x) => ({ ...x, source: r.source_id, folder }))} />)}</div>
            )}
          </>
        )}
      </div>
    </>
  );
}

function ResultCard({ r, q, onFolder }: { r: Result; q: string; onFolder: (folder: string) => void }) {
  const folder = r.path.split("/").slice(0, -1).join("/") || "/";
  return (
    <Card className="p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex min-w-0 items-center gap-2">
            {r.kind === "folder" ? <Folder className="h-4 w-4 shrink-0 text-muted" /> : <FileText className="h-4 w-4 shrink-0 text-muted" />}
            <span className="truncate font-semibold"><Highlight text={r.name} q={q} /></span>
            {r.match.map((m) => <span key={m} className={cx("shrink-0 rounded-full px-2 py-0.5 text-[11px] font-semibold", m === "content" ? "bg-accent-soft text-accent" : "bg-bg text-muted")}>
              {m === "content" ? "in contents" : "in name"}</span>)}
          </div>
          <div className="mt-0.5 flex flex-wrap items-center gap-x-1.5 text-[13px] text-muted">
            <button onClick={() => onFolder(folder)} title="Search only in this folder" className="truncate font-mono hover:text-accent hover:underline">{folder}</button>
            <span>· {r.source}{r.device ? ` (${r.device})` : ""}</span>
            {r.size != null && <span>· {bytes(r.size)}</span>}
            {r.modified_at && <span>· modified {new Date(r.modified_at).toLocaleDateString()}</span>}
          </div>
        </div>
        {r.web_link ? <a href={r.web_link} target="_blank" rel="noreferrer" className="inline-flex shrink-0 items-center gap-1 text-sm text-accent hover:underline">Open<ExternalLink className="h-3.5 w-3.5" /></a>
          : r.local_path ? <span className="hidden max-w-[220px] shrink-0 truncate font-mono text-xs text-faint sm:block" title={r.local_path}>{r.local_path}</span> : null}
      </div>
      {r.snippet && <p className="mt-2.5 rounded-lg bg-bg px-3 py-2 text-[13.5px] leading-relaxed text-ink/90"><Highlight text={r.snippet} q={q} /></p>}
      {!r.snippet && r.summary && <p className="mt-2 line-clamp-2 text-[13px] text-muted">{r.summary}</p>}
    </Card>
  );
}

function Sel({ value, onChange, options }: { value: string; onChange: (v: string) => void; options: [string, string][] }) {
  return (
    <select value={value} onChange={(e) => onChange(e.target.value)} className="rounded-lg border border-line bg-panel px-2.5 py-1.5 text-sm">
      {options.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
    </select>
  );
}
