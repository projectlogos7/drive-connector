const KEY = "nd_key";

export function getKey(): string | null {
  try { return localStorage.getItem(KEY); } catch { return null; }
}
export function setKey(k: string | null) {
  try { k ? localStorage.setItem(KEY, k) : localStorage.removeItem(KEY); } catch { /* private mode */ }
}

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}

type Listener = () => void;
const unauthorized = new Set<Listener>();
export function onUnauthorized(fn: Listener) { unauthorized.add(fn); return () => { unauthorized.delete(fn); }; }

export async function api<T = any>(path: string, init: RequestInit = {}): Promise<T> {
  const key = getKey();
  const res = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(key ? { Authorization: `Bearer ${key}` } : {}), ...(init.headers || {}) },
  });
  if (res.status === 401) {
    unauthorized.forEach((f) => f());
    throw new ApiError(401, "Please sign in again");
  }
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = (body as any)?.detail;
    throw new ApiError(res.status, typeof detail === "string" ? detail : res.statusText || "Request failed");
  }
  return body as T;
}

export const fetcher = (path: string) => api(path);

export const post = <T = any>(path: string, body?: unknown) =>
  api<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });
export const put = <T = any>(path: string, body?: unknown) => api<T>(path, { method: "PUT", body: JSON.stringify(body) });
export const patch = <T = any>(path: string, body?: unknown) => api<T>(path, { method: "PATCH", body: JSON.stringify(body) });
export const del = <T = any>(path: string) => api<T>(path, { method: "DELETE" });
