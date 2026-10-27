// control-plane-api client. Same-origin `/v1` (nginx proxies to REPLICANT_API_UPSTREAM). Local identity: the static dev
// users of replicant_dev_auth, sent as X-Replicant-Dev-User (M1 stand-in for the BFF session, ADR-006).

export interface DevUser { login: string; name: string; role: string }

export const DEV_USERS: DevUser[] = [
  { login: "grace", name: "Grace", role: "platform admin · approver" },
  { login: "alice", name: "Alice", role: "payments · engineer" },
  { login: "bob", name: "Bob", role: "payments · approver" },
  { login: "carol", name: "Carol", role: "replicant-admins · operator" },
  { login: "dave", name: "Dave", role: "payments · viewer" },
  { login: "erin", name: "Erin", role: "payments · tenant admin" },
];

const USER_KEY = "replicant.devUser";

export function getDevUser(): string {
  try {
    const v = localStorage.getItem(USER_KEY);
    if (v && DEV_USERS.some((u) => u.login === v)) return v;
  } catch { /* storage unavailable */ }
  return "grace";
}

export function setDevUser(login: string): void {
  try { localStorage.setItem(USER_KEY, login); } catch { /* storage unavailable */ }
}

export class ApiError extends Error {
  status: number;
  code: string;
  title: string;
  detail?: string;
  requestId?: string;
  body: Record<string, unknown>;
  constructor(status: number, body: Record<string, unknown>, requestId?: string) {
    const code = String(body.code ?? (status === 0 ? "network_error" : `http_${status}`));
    super(`${status} ${code}`);
    this.status = status;
    this.code = code;
    this.title = String(body.title ?? code.replace(/_/g, " "));
    this.detail = body.detail ? String(body.detail) : undefined;
    this.requestId = String(body.request_id ?? requestId ?? "") || undefined;
    this.body = body;
  }
}

export interface ApiResult<T> { data: T; status: number; headers: Headers }

export async function request<T>(path: string, init: RequestInit = {}): Promise<ApiResult<T>> {
  const headers = new Headers(init.headers);
  headers.set("X-Replicant-Dev-User", getDevUser());
  headers.set("Accept", "application/json");
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  let res: Response;
  try {
    res = await fetch(path, { ...init, headers, credentials: "same-origin" });
  } catch {
    throw new ApiError(0, { code: "network_error", title: "control-plane-api is not reachable" });
  }
  const rid = res.headers.get("X-Request-Id") ?? undefined;
  const text = await res.text();
  let body: unknown = null;
  try { body = text ? JSON.parse(text) : null; } catch { body = { detail: text.slice(0, 200) }; }
  if (!res.ok) throw new ApiError(res.status, (body as Record<string, unknown>) ?? {}, rid);
  return { data: body as T, status: res.status, headers: res.headers };
}

export const get = async <T>(path: string): Promise<T> => (await request<T>(path)).data;

export async function getBytes(path: string): Promise<Uint8Array> {
  const res = await fetch(path, { headers: { "X-Replicant-Dev-User": getDevUser() } });
  if (!res.ok) throw new ApiError(res.status, {}, res.headers.get("X-Request-Id") ?? undefined);
  return new Uint8Array(await res.arrayBuffer());
}

export function post<T>(path: string, body: unknown, idempotencyKey?: string): Promise<ApiResult<T>> {
  const headers: Record<string, string> = {};
  if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;
  return request<T>(path, { method: "POST", body: JSON.stringify(body), headers });
}

export const lagSeconds = (h: Headers) => Number(h.get("X-Replicant-Projection-Lag-Seconds") ?? "0") || 0;
