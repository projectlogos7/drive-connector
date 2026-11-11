"use client";
import { Mail } from "lucide-react";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { Button, Card } from "@/components/ui";
import { setKey } from "@/lib/api";

export default function Login() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const [key, setK] = useState("");
  const [keyErr, setKeyErr] = useState<string | null>(null);
  useEffect(() => {
    const h = decodeURIComponent(window.location.hash.slice(1));
    if (h.startsWith("error=")) { setMsg(h.slice(6)); history.replaceState(null, "", window.location.pathname); }
  }, []);

  async function sendLink(e: React.FormEvent) {
    e.preventDefault();
    if (!email.trim()) return;
    setSending(true); setMsg(null);
    try {
      const r = await fetch("/api/auth/request-link", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email }) });
      const body = await r.json().catch(() => ({}));
      if (!r.ok) setMsg(body.detail || "Could not send the link");
      else setMsg("If that address has access, a sign-in link is on its way. It works once and expires in 30 minutes."
        + (body.dev_links?.length ? ` Dev link: ${body.dev_links[0].url}` : ""));
    } catch { setMsg("Network error — try again"); } finally { setSending(false); }
  }
  async function useKey(e: React.FormEvent) {
    e.preventDefault();
    setKeyErr(null);
    const r = await fetch("/api/me", { headers: { Authorization: `Bearer ${key.trim()}` } });
    if (!r.ok) { setKeyErr("That key was not accepted"); return; }
    setKey(key.trim()); router.replace("/");
  }

  return (
    <div className="grid min-h-screen place-items-center p-4">
      <Card className="w-full max-w-sm p-7">
        <div className="flex items-center gap-2.5 font-semibold"><div className="grid h-7 w-7 place-items-center rounded-lg bg-accent text-sm text-accent-ink">N</div>Nous Drive</div>
        <h1 className="mt-6 text-xl font-semibold">Sign in</h1>
        <p className="mt-1 text-sm text-muted">We’ll email you a one-time sign-in link.</p>
        <form onSubmit={sendLink} className="mt-5 space-y-3">
          <input type="email" required value={email} onChange={(e) => setEmail(e.target.value)} placeholder="you@example.org" autoComplete="email"
            className="w-full rounded-lg border border-line bg-panel px-3 py-2.5 text-sm" />
          <Button type="submit" loading={sending} className="w-full py-2.5" icon={<Mail className="h-4 w-4" />}>Email me a link</Button>
        </form>
        {msg && <p className="mt-3 break-words text-sm text-muted">{msg}</p>}
        <details className="mt-6 text-sm">
          <summary className="cursor-pointer text-muted">Use an admin key instead</summary>
          <form onSubmit={useKey} className="mt-3 space-y-2">
            <input type="password" value={key} onChange={(e) => setK(e.target.value)} placeholder="nd_admin_…" className="w-full rounded-lg border border-line bg-panel px-3 py-2 font-mono text-sm" />
            <Button type="submit" variant="ghost" className="w-full">Sign in with key</Button>
            {keyErr && <p className="text-bad">{keyErr}</p>}
          </form>
        </details>
      </Card>
    </div>
  );
}
