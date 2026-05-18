// Typed client for the einkgen-read-api Lambda.
// All endpoints are read-only and public; no auth headers.

export interface QueueItem {
  id: string;
  enqueued_at: string;
  source: string;
  kind: "prompt" | "image" | "random";
  prompt?: string;
  image_s3_key?: string;
}

export interface QueueResponse {
  items: QueueItem[];
}

export interface HistorySource {
  kind: string;
  model?: string;
  prompt?: string;
}

export interface HistoryItem {
  id: string;
  generated_at: string;
  image_sha256: string;
  source: HistorySource;
}

export interface HistoryResponse {
  items: HistoryItem[];
}

export interface DeviceStatus {
  device_id: string;
  battery_v: number;
  battery_pct: number;
  rssi: number;
  current_hash: string;
  fw_version: string;
  last_seen: string;
}

export type StatusResult =
  | { kind: "ok"; status: DeviceStatus }
  | { kind: "no_status_yet" };

const READ_API_URL: string =
  (import.meta.env.VITE_READ_API_URL as string | undefined) ??
  "http://localhost:3001";

const CDN_BASE: string =
  (import.meta.env.VITE_CDN_BASE as string | undefined) ??
  "http://localhost:3001/cdn";

export function cdnUrl(path: string): string {
  const base = CDN_BASE.replace(/\/+$/, "");
  // Encode the path so filenames with spaces, #, ?, or unicode don't break
  // the <img src>. Operator-supplied filenames flow into queue/staged keys
  // verbatim today; encoding here is the cheap defensive layer.
  const tail = encodeURI(path.replace(/^\/+/, ""));
  return `${base}/${tail}`;
}

function apiUrl(path: string): string {
  const base = READ_API_URL.replace(/\/+$/, "");
  const tail = path.replace(/^\/+/, "");
  return `${base}/${tail}`;
}

async function fetchJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(apiUrl(path), { signal });
  if (!res.ok) {
    throw new Error(`GET ${path} failed: ${res.status} ${res.statusText}`);
  }
  return (await res.json()) as T;
}

export async function getQueue(signal?: AbortSignal): Promise<QueueResponse> {
  return fetchJson<QueueResponse>("/queue", signal);
}

export async function getHistory(
  limit: number,
  signal?: AbortSignal,
): Promise<HistoryResponse> {
  return fetchJson<HistoryResponse>(`/history?limit=${limit}`, signal);
}

// Current manifest — what the device is currently being told to draw.
// Served directly from CloudFront (public, 60s cache) without going through
// the read-api Lambda. See ARCHITECTURE §7.
export interface CurrentManifest {
  version: number;
  generated_at: string;
  image_url: string;
  image_sha256: string;
  image_bytes: number;
  next_check_after: string;
  source: HistorySource & { replayed_from?: string };
}

export async function getCurrentManifest(
  signal?: AbortSignal,
): Promise<CurrentManifest | null> {
  // Cache-bust so the eye indicator updates immediately after a "Show this"
  // click — CloudFront caches /current/manifest.json for up to 60s and we
  // already invalidate on publish, but a recently-rendered tab can still
  // have the stale body in the browser's HTTP cache.
  const url = cdnUrl(`current/manifest.json?ts=${Date.now()}`);
  const res = await fetch(url, { signal, cache: "no-store" });
  if (res.status === 404 || res.status === 403) return null;
  if (!res.ok) {
    throw new Error(
      `GET current/manifest.json failed: ${res.status} ${res.statusText}`,
    );
  }
  return (await res.json()) as CurrentManifest;
}

export async function getStatus(signal?: AbortSignal): Promise<StatusResult> {
  const res = await fetch(apiUrl("/status"), { signal });
  if (res.status === 404) {
    return { kind: "no_status_yet" };
  }
  if (!res.ok) {
    throw new Error(`GET /status failed: ${res.status} ${res.statusText}`);
  }
  const status = (await res.json()) as DeviceStatus;
  return { kind: "ok", status };
}

// ---------------------------------------------------------------------------
// Admin API — operator writes. Same-origin path under /admin/*, fronted by
// CloudFront. credentials: 'include' so the session cookie travels on every
// request.
// ---------------------------------------------------------------------------

// Override-able for `vite dev` against a deployed stack. In production the
// SPA and the admin API share an origin, so the empty default works.
const ADMIN_API_BASE: string =
  (import.meta.env.VITE_ADMIN_API_BASE as string | undefined) ?? "";

function adminUrl(path: string): string {
  const base = ADMIN_API_BASE.replace(/\/+$/, "");
  const tail = path.replace(/^\/+/, "");
  return `${base}/${tail}`;
}

async function adminFetch(path: string, init: RequestInit = {}): Promise<Response> {
  return fetch(adminUrl(path), {
    ...init,
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...(init.headers ?? {}),
    },
  });
}

export interface AdminMeOk {
  kind: "ok";
  sub: string;
  exp: number;
}
export type AdminMeResult =
  | AdminMeOk
  | { kind: "unauthenticated" }
  | { kind: "not_configured" };

export async function adminMe(signal?: AbortSignal): Promise<AdminMeResult> {
  const res = await adminFetch("/admin/me", { method: "GET", signal });
  if (res.status === 401) return { kind: "unauthenticated" };
  if (res.status === 503) return { kind: "not_configured" };
  if (!res.ok) {
    throw new Error(`GET /admin/me failed: ${res.status} ${res.statusText}`);
  }
  const data = (await res.json()) as { sub: string; exp: number };
  return { kind: "ok", sub: data.sub, exp: data.exp };
}

export async function adminLogin(password: string): Promise<void> {
  const res = await adminFetch("/admin/login", {
    method: "POST",
    body: JSON.stringify({ password }),
  });
  if (res.status === 401) {
    throw new Error("Incorrect password.");
  }
  if (res.status === 503) {
    throw new Error(
      "Admin password not configured. Run QUICKSTART §3.5 to set einkgen/admin_password.",
    );
  }
  if (!res.ok) {
    throw new Error(`Login failed: ${res.status} ${res.statusText}`);
  }
}

export async function adminLogout(): Promise<void> {
  await adminFetch("/admin/logout", { method: "POST" });
}

export interface AdminEnqueueResponse {
  id: string;
  kind: string;
}

export async function adminEnqueuePrompt(prompt: string): Promise<AdminEnqueueResponse> {
  const res = await adminFetch("/admin/queue/prompt", {
    method: "POST",
    body: JSON.stringify({ prompt }),
  });
  if (res.status === 401) throw new Error("Session expired. Please log in again.");
  if (!res.ok) {
    const detail = await res.text();
    throw new Error(`Enqueue failed: ${res.status} ${detail}`);
  }
  return (await res.json()) as AdminEnqueueResponse;
}

export interface AdminPromptsResponse {
  prompts: string[];
  is_default: boolean;
  defaults?: string[];
}

export async function adminGetPrompts(
  signal?: AbortSignal,
): Promise<AdminPromptsResponse> {
  const res = await adminFetch("/admin/prompts", { method: "GET", signal });
  if (res.status === 401) throw new Error("Session expired. Please log in again.");
  if (!res.ok) {
    throw new Error(`GET /admin/prompts failed: ${res.status} ${res.statusText}`);
  }
  return (await res.json()) as AdminPromptsResponse;
}

export async function adminPutPrompts(
  prompts: string[],
): Promise<AdminPromptsResponse> {
  const res = await adminFetch("/admin/prompts", {
    method: "PUT",
    body: JSON.stringify({ prompts }),
  });
  if (res.status === 401) throw new Error("Session expired. Please log in again.");
  if (!res.ok) {
    const detail = await res.text();
    throw new Error(`Save failed: ${res.status} ${detail}`);
  }
  return (await res.json()) as AdminPromptsResponse;
}

export async function adminResetPrompts(): Promise<AdminPromptsResponse> {
  const res = await adminFetch("/admin/prompts/reset", { method: "POST" });
  if (res.status === 401) throw new Error("Session expired. Please log in again.");
  if (!res.ok) {
    const detail = await res.text();
    throw new Error(`Reset failed: ${res.status} ${detail}`);
  }
  return (await res.json()) as AdminPromptsResponse;
}

export interface AdminShowResponse {
  version: number;
  image_sha256: string;
  history_id: string;
}

export async function adminShowHistory(
  historyId: string,
): Promise<AdminShowResponse> {
  const res = await adminFetch("/admin/show", {
    method: "POST",
    body: JSON.stringify({ history_id: historyId }),
  });
  if (res.status === 401) throw new Error("Session expired. Please log in again.");
  if (res.status === 404) throw new Error("That history item no longer exists.");
  if (!res.ok) {
    const detail = await res.text();
    throw new Error(`Show failed: ${res.status} ${detail}`);
  }
  return (await res.json()) as AdminShowResponse;
}

export interface AdminFailureItem {
  id: string;
  enqueued_at: string;
  recorded_at: string;
  source: string;
  kind: string;
  reason: string;
  prompt: string | null;
}

export interface AdminFailuresResponse {
  items: AdminFailureItem[];
}

export async function adminGetFailures(
  signal?: AbortSignal,
): Promise<AdminFailuresResponse> {
  const res = await adminFetch("/admin/failures", { method: "GET", signal });
  if (res.status === 401) throw new Error("Session expired. Please log in again.");
  if (!res.ok) {
    throw new Error(`GET /admin/failures failed: ${res.status} ${res.statusText}`);
  }
  return (await res.json()) as AdminFailuresResponse;
}

export async function adminEnqueueImage(
  file: File,
  prompt: string | null,
): Promise<AdminEnqueueResponse> {
  const bytes = new Uint8Array(await file.arrayBuffer());
  // Build base64 in chunks so we don't blow the call-stack limit on phone-size
  // photos (Uint8Array → String.fromCharCode.apply blows up past ~120k args).
  let binary = "";
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode.apply(
      null,
      Array.from(bytes.subarray(i, i + chunk)),
    );
  }
  const image_b64 = btoa(binary);
  const res = await adminFetch("/admin/queue/image", {
    method: "POST",
    body: JSON.stringify({
      filename: file.name || "image",
      image_b64,
      prompt: prompt ?? undefined,
    }),
  });
  if (res.status === 401) throw new Error("Session expired. Please log in again.");
  if (res.status === 413) throw new Error("Image too large (max ~8 MB).");
  if (!res.ok) {
    const detail = await res.text();
    throw new Error(`Upload failed: ${res.status} ${detail}`);
  }
  return (await res.json()) as AdminEnqueueResponse;
}
