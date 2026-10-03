import { describe, expect, it } from "vitest";

import { DEFAULT_AFTER_LOGIN, loginPath, safeNextPath } from "./safe-redirect";

describe("safeNextPath", () => {
  it.each([
    ["/leads", "/leads"],
    ["/admin/users?status=invited", "/admin/users?status=invited"],
    ["/settings#password", "/settings#password"],
  ])("keeps same-origin paths: %s", (next, expected) => {
    expect(safeNextPath(next)).toBe(expected);
  });

  it.each([
    null,
    "",
    "https://evil.example/phish",
    "//evil.example",
    "/\\evil.example",
    "\\\\evil.example",
    "/\t/evil.example",
    "javascript:alert(1)",
    "leads",
    "/login",
    "/activate/abc",
    "/reset-password/abc",
    "/" + "x".repeat(2100),
    // Review P1: dot segments normalise into a protocol-relative URL.
    "/.//evil.com",
    "/..//evil.com",
    "/%2e//evil.com",
    "/%2E%2E//evil.com/phish",
    "/leads/..//evil.com",
    "/./\\evil.com",
  ])("rejects anything that could leave the site or loop: %s", (next) => {
    expect(safeNextPath(next)).toBe(DEFAULT_AFTER_LOGIN);
  });

  it("returns only paths that stay on this origin even after the browser resolves them", () => {
    for (const next of ["/.//evil.com", "/a/b/../../..//evil.com", "/%2e%2e/%2e%2e//evil.com"]) {
      const result = safeNextPath(next);
      expect(new URL(result, "https://crm.example").origin).toBe("https://crm.example");
    }
  });

  it("resolves harmless dot segments", () => {
    expect(safeNextPath("/admin/../leads")).toBe("/leads");
  });
});

describe("loginPath", () => {
  it("carries a safe next path and a reason", () => {
    expect(loginPath("/leads?x=1", "expired")).toBe("/login?next=%2Fleads%3Fx%3D1&reason=expired");
  });

  it("drops unsafe or default next paths", () => {
    expect(loginPath("//evil.example")).toBe("/login");
    expect(loginPath("/.//evil.example", "expired")).toBe("/login?reason=expired");
    expect(loginPath("/dashboard")).toBe("/login");
  });
});
