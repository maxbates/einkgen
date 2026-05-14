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
  const tail = path.replace(/^\/+/, "");
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
