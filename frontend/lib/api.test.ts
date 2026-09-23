/**
 * The frontend had no unit tests at all — its only safety net was 8 Playwright
 * smoke tests against a full running stack, which cannot easily provoke a 503
 * or a network failure.
 *
 * What is tested here is the rule that actually matters for correctness:
 * a GET may be retried, a POST/PATCH/DELETE may NOT. A mutation that reached
 * the server may already have taken effect, so a silent retry can double-fire
 * a non-idempotent action — create two incidents, dismiss twice, purge twice.
 * That rule lives in a comment and in one boolean; nothing verified it.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, api, checkBackendIdentity, BACKEND_SERVICE_NAME } from "./api";

function jsonResponse(status: number, body: unknown = {}): Response {
  return {
    status,
    // `ok` is what api.ts branches on for the non-401 error path; omitting it
    // makes even a 200 look like a failure.
    ok: status >= 200 && status < 300,
    statusText: String(status),
    headers: { get: () => "application/json" },
    json: async () => body,
  } as unknown as Response;
}

beforeEach(() => {
  vi.useFakeTimers();
  // A token would send the 401 path into a location redirect; these tests are
  // about transport behaviour, so stay logged out.
  vi.stubGlobal("localStorage", { getItem: () => null, setItem: () => {}, removeItem: () => {} });
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

/** Runs `promise` while draining the retry backoff timers. */
async function withTimers<T>(promise: Promise<T>): Promise<T> {
  const settled = promise.then(
    (value) => ({ ok: true as const, value }),
    (error) => ({ ok: false as const, error }),
  );
  await vi.runAllTimersAsync();
  const outcome = await settled;
  if (outcome.ok) return outcome.value;
  throw outcome.error;
}

describe("transient-failure retry", () => {
  it("retries a GET on a retryable status and returns the eventual success", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(503))
      .mockResolvedValueOnce(jsonResponse(200, { ok: true }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(withTimers(api.get("/api/anything"))).resolves.toEqual({ ok: true });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("gives up after a bounded number of attempts", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(503, { detail: "still down" }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(withTimers(api.get("/api/anything"))).rejects.toBeInstanceOf(ApiError);
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it.each([
    ["post", (p: string) => api.post(p, {})],
    ["patch", (p: string) => api.patch(p, {})],
    ["del", (p: string) => api.del(p)],
  ])("never retries a %s, which may already have taken effect", async (_name, call) => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(503, { detail: "down" }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(withTimers(call("/api/incidents"))).rejects.toBeInstanceOf(ApiError);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("does not retry a status outside the transient set", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(404, { detail: "missing" }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(withTimers(api.get("/api/nope"))).rejects.toBeInstanceOf(ApiError);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("retries a network failure, because nothing reached the server", async () => {
    const fetchMock = vi
      .fn()
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockResolvedValueOnce(jsonResponse(200, { recovered: true }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(withTimers(api.get("/api/anything"))).resolves.toEqual({ recovered: true });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("surfaces the server's detail message rather than a generic failure", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(400, { detail: "Zone box is inverted" })));

    await expect(withTimers(api.get("/api/zones"))).rejects.toMatchObject({
      status: 400,
      message: "Zone box is inverted",
    });
  });
});

/**
 * The backend-identity preflight. The bug it exists for: an unrelated local
 * Python app held port 8000 — the documented default — so the dashboard was
 * pointed at a stranger's API and reported only "Login failed". Nothing
 * distinguished a wrong password from a wrong server.
 */
describe("backend identity preflight", () => {
  function healthResponse(status: number, body: unknown, jsonThrows = false): Response {
    return {
      status,
      ok: status >= 200 && status < 300,
      statusText: String(status),
      headers: { get: () => "application/json" },
      json: async () => {
        if (jsonThrows) throw new SyntaxError("Unexpected token <");
        return body;
      },
    } as unknown as Response;
  }

  it("accepts the real backend", async () => {
    const fetchMock = vi.fn().mockResolvedValue(healthResponse(200, { ok: true, service: BACKEND_SERVICE_NAME }));
    await expect(checkBackendIdentity("http://localhost:8000", fetchMock)).resolves.toEqual({ status: "ok" });
  });

  it("reports nothing listening as unreachable, naming the base URL", async () => {
    const fetchMock = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    const result = await checkBackendIdentity("http://localhost:8000", fetchMock);
    expect(result.status).toBe("unreachable");
    expect(result.status !== "ok" && result.message).toContain("http://localhost:8000");
  });

  it("rejects a different app that answers on the same port", async () => {
    // The actual failure: some other service replies 200 JSON on /api/health.
    const fetchMock = vi.fn().mockResolvedValue(healthResponse(200, { ok: true, service: "some-other-app" }));
    const result = await checkBackendIdentity("http://localhost:8000", fetchMock);
    expect(result.status).toBe("wrong-service");
    expect(result.status !== "ok" && result.message).toContain("some-other-app");
  });

  it("rejects an answer with no service name rather than assuming it is ours", async () => {
    const fetchMock = vi.fn().mockResolvedValue(healthResponse(200, { ok: true }));
    expect((await checkBackendIdentity("http://localhost:8000", fetchMock)).status).toBe("wrong-service");
  });

  it("rejects a non-JSON body, which is what an unrelated web app serves", async () => {
    const fetchMock = vi.fn().mockResolvedValue(healthResponse(200, null, true));
    expect((await checkBackendIdentity("http://localhost:8000", fetchMock)).status).toBe("wrong-service");
  });

  it("treats a 404 on /api/health as the wrong service, not a dead one", async () => {
    const fetchMock = vi.fn().mockResolvedValue(healthResponse(404, { detail: "Not Found" }));
    const result = await checkBackendIdentity("http://localhost:8000", fetchMock);
    expect(result.status).toBe("wrong-service");
    expect(result.status !== "ok" && result.message).toContain("404");
  });

  it("never throws, because its only job is to produce a readable message", async () => {
    const fetchMock = vi.fn().mockRejectedValue(new Error("boom"));
    await expect(checkBackendIdentity("http://localhost:9999", fetchMock)).resolves.toBeTruthy();
  });

  it("does not retry — a wrong app answers instantly and consistently", async () => {
    const fetchMock = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    await checkBackendIdentity("http://localhost:8000", fetchMock);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("sends no Authorization header — it runs before anyone has logged in", async () => {
    const fetchMock = vi.fn().mockResolvedValue(healthResponse(200, { service: BACKEND_SERVICE_NAME }));
    await checkBackendIdentity("http://localhost:8000", fetchMock);
    const init = fetchMock.mock.calls[0][1];
    expect(init?.headers).toBeUndefined();
  });
});
