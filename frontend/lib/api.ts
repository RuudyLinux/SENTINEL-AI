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

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const token = getToken();
  const headers: Record<string, string> = { ...(options.headers as any) };
  if (!(options.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
  }
  if (token) headers["Authorization"] = `Bearer ${token}`;

  const res = await fetch(`${API_BASE}${path}`, { ...options, headers });
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

export async function openTokenedResource(tokenPath: string, resourcePath: string): Promise<void> {
  const url = await buildTokenedUrl(tokenPath, resourcePath);
  window.open(url, "_blank");
}

export { ApiError };
