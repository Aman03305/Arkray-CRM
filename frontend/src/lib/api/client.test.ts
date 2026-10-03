import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, apiFetch, readCookie } from "./client";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

describe("apiFetch", () => {
  const fetchMock = vi.fn<typeof fetch>();

  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
    document.cookie = "arkray_csrftoken=csrf-123";
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    fetchMock.mockReset();
    document.cookie = "arkray_csrftoken=; expires=Thu, 01 Jan 1970 00:00:00 GMT";
  });

  it("sends same-origin credentials and parses JSON", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, { status: "ok" }));
    await expect(apiFetch("/api/v1/workspaces/me/dashboard")).resolves.toEqual({ status: "ok" });
    const [, init] = fetchMock.mock.calls[0]!;
    expect(init?.credentials).toBe("same-origin");
    expect((init?.headers as Record<string, string>)["X-CSRFToken"]).toBeUndefined();
  });

  it("adds the CSRF token and JSON body to unsafe requests", async () => {
    fetchMock.mockResolvedValue(jsonResponse(201, { id: "l1" }));
    await apiFetch("/api/v1/workspaces/me/leads", { method: "POST", body: { first_name: "Rahul" } });
    const [, init] = fetchMock.mock.calls[0]!;
    const headers = init?.headers as Record<string, string>;
    expect(headers["X-CSRFToken"]).toBe("csrf-123");
    expect(headers["Content-Type"]).toBe("application/json");
    expect(init?.body).toBe(JSON.stringify({ first_name: "Rahul" }));
  });

  it("refuses to call anything other than the same-origin API", async () => {
    await expect(apiFetch("https://evil.example/api/v1/x")).rejects.toThrow(/same-origin/);
    await expect(apiFetch("//evil.example/api")).rejects.toThrow(/same-origin/);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("maps the backend error envelope to ApiError", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(404, { error: { code: "not_found", message: "Not found.", details: null, request_id: "req-1" } }),
    );
    const error = await apiFetch("/api/v1/workspaces/me/leads/x").catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 404, code: "not_found", message: "Not found.", requestId: "req-1" });
  });

  it("handles non-JSON error pages (e.g. a proxy's 502) gracefully", async () => {
    fetchMock.mockResolvedValue(new Response("<html>Bad gateway</html>", { status: 502, headers: { "Content-Type": "text/html" } }));
    await expect(apiFetch("/api/v1/x")).rejects.toMatchObject({ status: 502, code: "http_error" });
  });

  it("reports network failures without leaking internals", async () => {
    fetchMock.mockRejectedValue(new TypeError("Failed to fetch"));
    await expect(apiFetch("/api/v1/x")).rejects.toMatchObject({ code: "network_error", status: 0 });
  });

  it("times out instead of hanging", async () => {
    fetchMock.mockImplementation(
      (_url, init) =>
        new Promise((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
        }),
    );
    await expect(apiFetch("/api/v1/x", { timeoutMs: 20 })).rejects.toMatchObject({ code: "timeout" });
  });

  it("returns undefined for 204 No Content", async () => {
    fetchMock.mockResolvedValue(new Response(null, { status: 204 }));
    await expect(apiFetch("/api/v1/x", { method: "DELETE" })).resolves.toBeUndefined();
  });
});

describe("readCookie", () => {
  it("finds a cookie among several and decodes it", () => {
    expect(readCookie("b", "a=1; b=hello%20world; c=3")).toBe("hello world");
    expect(readCookie("missing", "a=1")).toBeNull();
  });
});

describe("CSRF handling", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("fetches the CSRF cookie first when it is missing", async () => {
    const fetchMock = vi.fn<typeof fetch>(async (input) => {
      if (String(input) === "/api/v1/auth/csrf") {
        document.cookie = "arkray_csrftoken=fresh-token";
        return new Response(null, { status: 204 });
      }
      return jsonResponse(200, { ok: true });
    });
    vi.stubGlobal("fetch", fetchMock);

    await apiFetch("/api/v1/auth/login", { method: "POST", body: {} });

    expect(fetchMock.mock.calls.map(([url]) => String(url))).toEqual(["/api/v1/auth/csrf", "/api/v1/auth/login"]);
    const headers = fetchMock.mock.calls[1]![1]?.headers as Record<string, string>;
    expect(headers["X-CSRFToken"]).toBe("fresh-token");
  });

  it("retries once with a fresh token after a csrf_failed rejection", async () => {
    document.cookie = "arkray_csrftoken=stale-token";
    const responses = [
      jsonResponse(403, { error: { code: "csrf_failed", message: "CSRF verification failed.", details: null, request_id: "r1" } }),
      jsonResponse(200, { ok: true }),
    ];
    const fetchMock = vi.fn<typeof fetch>(async (input) => {
      if (String(input) === "/api/v1/auth/csrf") {
        document.cookie = "arkray_csrftoken=rotated-token";
        return new Response(null, { status: 204 });
      }
      return responses.shift()!;
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(apiFetch("/api/v1/auth/logout", { method: "POST" })).resolves.toEqual({ ok: true });
    const tokens = fetchMock.mock.calls
      .filter(([url]) => String(url) !== "/api/v1/auth/csrf")
      .map(([, init]) => (init?.headers as Record<string, string>)["X-CSRFToken"]);
    expect(tokens).toEqual(["stale-token", "rotated-token"]);
  });

  it("does not retry other 403s", async () => {
    document.cookie = "arkray_csrftoken=token";
    const fetchMock = vi.fn<typeof fetch>(async () =>
      jsonResponse(403, { error: { code: "permission_denied", message: "No.", details: null, request_id: "r1" } }),
    );
    vi.stubGlobal("fetch", fetchMock);
    await expect(apiFetch("/api/v1/admin/users", { method: "POST", body: {} })).rejects.toMatchObject({ status: 403 });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("exposes Retry-After on rate-limit errors", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>(
        async () =>
          new Response(
            JSON.stringify({ error: { code: "rate_limited", message: "Slow down.", details: null, request_id: "r" } }),
            { status: 429, headers: { "Content-Type": "application/json", "Retry-After": "90" } },
          ),
      ),
    );
    await expect(apiFetch("/api/v1/x")).rejects.toMatchObject({ code: "rate_limited", retryAfterSeconds: 90 });
  });
});
