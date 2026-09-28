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

// Bounded retry on transient errors, GET only: a mutation that reached the
// server may already have happened. Network failures with no response are
// retried for any method, since the request never left the browser.
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
  // unreachable, the loop returns or throws on the last attempt; keeps tsc happy
  throw lastErr ?? new Error("request failed");
}

// FastAPI's 422 detail is a list of {loc, msg}; render it as readable text.
export function formatDetail(detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((e: any) => {
        const field = (Array.isArray(e?.loc) ? e.loc : []).filter((p: unknown) => p !== "body").join(".");
        const msg = e?.msg ?? String(e);
        return field ? `${field.replace(/_/g, " ")}: ${msg}` : msg;
      })
      .join("; ");
  }
  return JSON.stringify(detail);
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
    // A 401 from the login endpoint means wrong credentials, not an expired
    // session, so only other endpoints clear the token and redirect.
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
      detail = formatDetail(body.detail);
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

// Evidence files and camera streams load via <img src>, <a href> or
// window.open, which can't send an Authorization header, so they use a
// short-lived resource token appended as ?token=.
export async function fetchResourceToken(tokenPath: string): Promise<string> {
  const { token } = await api.get<{ token: string }>(tokenPath);
  return token;
}

export async function buildTokenedUrl(tokenPath: string, resourcePath: string): Promise<string> {
  const token = await fetchResourceToken(tokenPath);
  const sep = resourcePath.includes("?") ? "&" : "?";
  return `${API_BASE}${resourcePath}${sep}token=${encodeURIComponent(token)}`;
}

/** A resource token's expiry in epoch ms, or null. For scheduling only; the
 *  backend enforces expiry. */
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

/** buildTokenedUrl plus the time the caller must refresh by. The backend ends
 *  an MJPEG stream at token expiry; the timestamp in the new URL makes <img>
 *  reconnect. */
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


// Backend identity preflight: /api/health returns a service name, so another
// app listening on NEXT_PUBLIC_API_BASE is reported clearly instead of as
// random 404/422 errors. No token, no retry, never throws.
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
  // The name must match, not merely be present.
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
