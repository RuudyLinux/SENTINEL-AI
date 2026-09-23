export const API_BASE = process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8000";
export const WS_BASE = process.env.NEXT_PUBLIC_WS_BASE || "ws://localhost:8000";

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem("sentinel_token");
}

export function setToken(token: string) {
  localStorage.setItem("sentinel_token", token);
}

export function clearToken() {
  localStorage.removeItem("sentinel_token");
}

export function getStoredUser(): any | null {
  if (typeof window === "undefined") return null;
  const raw = localStorage.getItem("sentinel_user");
  return raw ? JSON.parse(raw) : null;
}

export function setStoredUser(user: any) {
  localStorage.setItem("sentinel_user", JSON.stringify(user));
}

class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

// Self-Heal "API transient errors" recovery (spec recovery type 6): a
// bounded retry, only for the standard transient set, only for GET — a
// POST/PATCH/DELETE that reached the server may have already taken effect,
// so auto-retrying those here could double-fire a non-idempotent action
// (e.g. creating a duplicate incident); callers that need a retry for those
// offer it explicitly (a Retry button), never silently. A network-level
// fetch failure (no response at all) is retried for any method that never
// left the browser — nothing on the server could have run yet.
const RETRYABLE_STATUS = new Set([408, 429, 500, 502, 503, 504]);
const MAX_FETCH_ATTEMPTS = 3;
const RETRY_BACKOFF_MS = [300, 900]; // between attempts 1->2 and 2->3

async function _fetchWithRetry(url: string, init: RequestInit, method: string): Promise<Response> {
  let lastErr: unknown = null;
  for (let attempt = 1; attempt <= MAX_FETCH_ATTEMPTS; attempt++) {
    try {
      const res = await fetch(url, init);
      const canRetryStatus = method === "GET" && RETRYABLE_STATUS.has(res.status);
      if (!canRetryStatus || attempt === MAX_FETCH_ATTEMPTS) return res;
    } catch (err) {
      lastErr = err;
      if (attempt === MAX_FETCH_ATTEMPTS) throw err;
    }
    await new Promise((r) => setTimeout(r, RETRY_BACKOFF_MS[Math.min(attempt - 1, RETRY_BACKOFF_MS.length - 1)]));
  }
  // Unreachable in practice (the loop above always returns or throws on the
  // final attempt) — satisfies the type checker without changing behavior.
  throw lastErr ?? new Error("request failed");
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const token = getToken();
  const headers: Record<string, string> = { ...(options.headers as any) };
  if (!(options.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
  }
  if (token) headers["Authorization"] = `Bearer ${token}`;

  const method = (options.method || "GET").toUpperCase();
  const res = await _fetchWithRetry(`${API_BASE}${path}`, { ...options, headers }, method);
  if (res.status === 401) {
    // The login endpoint itself returning 401 means "wrong credentials" —
    // a normal, expected business response, not a "your session expired"
    // signal. Treating it the same as every other 401 (clear token, hard-
    // redirect to /login) used to fire here too: the redirect discarded
    // the login page's in-flight React state before its own catch block
    // could ever call setError(), so a wrong password silently reset the
    // form with no message at all. Only the session-expiry case gets the
    // redirect; the login endpoint just throws, same as any other error,
    // so the caller's own error handling (and message) actually shows.
    if (path !== "/api/auth/login") {
      clearToken();
      if (typeof window !== "undefined") window.location.href = "/login";
    }
    let detail = "Unauthorized";
    try {
      const body = await res.json();
      detail = typeof body.detail === "string" ? body.detail : detail;
    } catch {}
    throw new ApiError(401, detail);
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {}
    throw new ApiError(res.status, detail);
  }
  const contentType = res.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    return res.json();
  }
  return undefined as unknown as T;
}

export const api = {
  get: <T,>(path: string) => request<T>(path, { method: "GET" }),
  post: <T,>(path: string, body?: any) =>
    request<T>(path, { method: "POST", body: body instanceof FormData ? body : JSON.stringify(body ?? {}) }),
  patch: <T,>(path: string, body?: any) =>
    request<T>(path, { method: "PATCH", body: body instanceof FormData ? body : JSON.stringify(body ?? {}) }),
  del: <T,>(path: string) => request<T>(path, { method: "DELETE" }),
};

// Evidence file/package and camera stream endpoints are hit via plain
// <img src>/<a href>/window.open — browsers can't attach an Authorization
// header to those, so the backend hands out a short-lived resource token
// via a normal authenticated request first (P0-E). These helpers do that
// token fetch, then build/open the real URL with `?token=` appended.
export async function fetchResourceToken(tokenPath: string): Promise<string> {
  const { token } = await api.get<{ token: string }>(tokenPath);
  return token;
}

export async function buildTokenedUrl(tokenPath: string, resourcePath: string): Promise<string> {
  const token = await fetchResourceToken(tokenPath);
  const sep = resourcePath.includes("?") ? "&" : "?";
  return `${API_BASE}${resourcePath}${sep}token=${encodeURIComponent(token)}`;
}

/** A resource token's expiry, in epoch ms, or null if it cannot be read.
 *  Read for SCHEDULING only — the backend enforces the same `exp`; a client
 *  that miscomputes this gets a dropped stream, never extra access. */
function tokenExpiresAt(token: string): number | null {
  try {
    const payload = token.split(".")[1];
    if (!payload) return null;
    const { exp } = JSON.parse(atob(payload.replace(/-/g, "+").replace(/_/g, "/")));
    return typeof exp === "number" ? exp * 1000 : null;
  } catch {
    return null;
  }
}

/** Like buildTokenedUrl, plus the deadline the caller must refresh by.
 *  The backend now ends an MJPEG stream when its token expires (a stream
 *  authorized once used to run indefinitely), so a viewer left open past the
 *  token TTL needs a fresh URL or the picture simply stops updating. The
 *  timestamp makes the URL differ, which is what forces <img> to reconnect
 *  rather than sit on the closed stream. */
export async function buildTokenedStream(
  tokenPath: string,
  resourcePath: string,
): Promise<{ url: string; expiresAt: number | null }> {
  const token = await fetchResourceToken(tokenPath);
  const sep = resourcePath.includes("?") ? "&" : "?";
  return {
    url: `${API_BASE}${resourcePath}${sep}token=${encodeURIComponent(token)}&reconnect=${Date.now()}`,
    expiresAt: tokenExpiresAt(token),
  };
}

export async function openTokenedResource(tokenPath: string, resourcePath: string): Promise<void> {
  const url = await buildTokenedUrl(tokenPath, resourcePath);
  window.open(url, "_blank");
}


// --- Backend identity preflight -------------------------------------------
// The dashboard talks to whatever is listening on NEXT_PUBLIC_API_BASE, and
// port 8000 is a common default that other local Python/dev servers also
// claim. When an UNRELATED app held 8000, every call still "worked" at the
// transport level and the dashboard failed with assorted 404/422 noise from a
// stranger's API — the one thing it never said is the only thing that was
// wrong: this is not the SENTINEL backend.
//
// `/api/health` already returns a service name, so identity is checkable. The
// check is deliberately separate from `request()`: it takes no token, is not
// retried (a wrong app answers instantly and consistently — retrying only
// delays the message), and it never throws, because its whole job is to
// TURN a failure into a readable string.
export const BACKEND_SERVICE_NAME = "sentinel-vision-backend";

export type ApiPreflight =
  | { status: "ok" }
  | { status: "unreachable"; message: string }
  | { status: "wrong-service"; message: string };

export async function checkBackendIdentity(
  base: string = API_BASE,
  fetchImpl: typeof fetch = fetch,
): Promise<ApiPreflight> {
  let res: Response;
  try {
    res = await fetchImpl(`${base}/api/health`, { method: "GET" });
  } catch {
    return {
      status: "unreachable",
      message: `No API is answering at ${base}. Start the backend, or point NEXT_PUBLIC_API_BASE at the port it is really on.`,
    };
  }
  if (!res.ok) {
    return {
      status: "wrong-service",
      message: `${base} answered ${res.status} on /api/health. Something is listening there, but it is not the SENTINEL backend.`,
    };
  }
  let body: any = null;
  try {
    body = await res.json();
  } catch {
    body = null;
  }
  // A different app that happens to serve JSON (or HTML) on this path is the
  // exact case being caught, so the service name must MATCH, not merely be
  // absent-and-assumed-fine.
  if (!body || body.service !== BACKEND_SERVICE_NAME) {
    const saw = body && typeof body.service === "string" ? `"${body.service}"` : "no service name";
    return {
      status: "wrong-service",
      message: `${base} is answering, but it is not the SENTINEL backend (expected "${BACKEND_SERVICE_NAME}", got ${saw}). Another application is probably holding that port.`,
    };
  }
  return { status: "ok" };
}

export { ApiError };
