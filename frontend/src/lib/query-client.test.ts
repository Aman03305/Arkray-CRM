import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "./api/client";
import { createQueryClient, isPublicPath, retryDelay, shouldRetry, VIEWER_QUERY_KEY } from "./query-client";

const browser = vi.hoisted(() => ({ hardNavigate: vi.fn(), location: "/leads?status=new" }));
vi.mock("./browser", () => ({
  hardNavigate: browser.hardNavigate,
  currentLocation: () => browser.location,
}));

function setPathname(pathname: string) {
  window.history.replaceState(null, "", pathname);
}

describe("retry policy", () => {
  it("never retries client errors", () => {
    expect(shouldRetry(0, new ApiError(400, "validation_error", "x"))).toBe(false);
    expect(shouldRetry(0, new ApiError(401, "not_authenticated", "x"))).toBe(false);
    expect(shouldRetry(0, new ApiError(404, "not_found", "x"))).toBe(false);
  });

  it("retries network and server errors twice", () => {
    const error = new ApiError(0, "network_error", "x");
    expect([0, 1, 2].map((n) => shouldRetry(n, error))).toEqual([true, true, false]);
    expect(shouldRetry(0, new ApiError(503, "service_unavailable", "x"))).toBe(true);
  });

  it("never retries a request that timed out (it already waited 15 s)", () => {
    expect(shouldRetry(0, new ApiError(0, "timeout", "x"))).toBe(false);
  });

  it("follows Retry-After: a short one is waited for, a long one isn't retried", () => {
    const brief = new ApiError(503, "service_unavailable", "x", null, null, 3);
    const long = new ApiError(503, "service_unavailable", "x", null, null, 30);
    expect(shouldRetry(0, brief)).toBe(true);
    expect(retryDelay(0, brief)).toBe(3000);
    expect(shouldRetry(0, long)).toBe(false);
    expect(retryDelay(1, new ApiError(0, "network_error", "x"))).toBe(2000);
  });
});

describe("a 401 on a signed-in page", () => {
  beforeEach(() => {
    browser.hardNavigate.mockReset();
    setPathname("/leads");
  });
  afterEach(() => setPathname("/"));

  async function failWith(error: ApiError, client = createQueryClient()) {
    await client
      .fetchQuery({ queryKey: ["x", Math.random()], queryFn: () => Promise.reject(error), retry: false })
      .catch(() => undefined);
    return client;
  }
  const unauthenticated = new ApiError(401, "not_authenticated", "x");

  it("reloads onto sign-in and comes back afterwards", async () => {
    await failWith(unauthenticated);
    expect(browser.hardNavigate).toHaveBeenCalledWith("/login?next=%2Fleads%3Fstatus%3Dnew", undefined);
  });

  it("says the session ended when the user had been signed in", async () => {
    const client = createQueryClient();
    client.setQueryData(VIEWER_QUERY_KEY, { id: "u1" });
    await failWith(unauthenticated, client);
    expect(browser.hardNavigate).toHaveBeenCalledWith(expect.stringContaining("reason=expired"), "signed-out");
  });

  it("redirects only once however many requests fail", async () => {
    const client = await failWith(unauthenticated);
    await failWith(unauthenticated, client);
    expect(browser.hardNavigate).toHaveBeenCalledTimes(1);
  });

  it("does nothing on public pages (no redirect loops)", async () => {
    setPathname("/login");
    await failWith(unauthenticated);
    expect(browser.hardNavigate).not.toHaveBeenCalled();
  });

  it("ignores other errors", async () => {
    await failWith(new ApiError(403, "permission_denied", "x"));
    expect(browser.hardNavigate).not.toHaveBeenCalled();
  });
});

it("knows the public authentication pages", () => {
  expect(["/login", "/forgot-password", "/reset-password/abc", "/activate/abc"].every(isPublicPath)).toBe(true);
  expect(["/dashboard", "/admin/users", "/loginx"].some(isPublicPath)).toBe(false);
});
