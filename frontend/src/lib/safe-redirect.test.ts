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
    "/unlock-sign-in/abc", // review round 1: an emailed one-time link's page
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

  // Adversarial review of 73ff1e3, L9: `/login?next=/api/v1/workspaces/<id>` made a signed-in
  // administrator's browser send a same-origin GET to the API after sign-in (past the
  // cross-site read guard, writing `workspace.accessed` in their name). Only pages are targets.
  it.each([
    "/api",
    "/api/",
    "/api/v1/workspaces/0b8f9a9e-1d1e-4c55-9f3a-2f2b5f1e7a10",
    "/api/v1/admin/privacy/exports?x=1",
    "/API/v1/auth/me",
    "/Api/v1/auth/me",
    "/%61pi/v1/auth/me",
    "/%2561pi/v1/auth/me",
    "/a%70i/v1/auth/me",
    "/leads/../api/v1/auth/me",
    "/leads/%2e%2e/api/v1/auth/me",
    "/./api/v1/auth/me",
    "/api%2fv1%2fauth%2fme",
    "/api;x/v1/auth/me",
    "/_next/static/chunks/main.js",
    "/_NEXT/image?url=x",
    "/%5fnext/static/x.js",
    "/health/live",
    "/icon.svg",
    "/favicon.ico",
    "/robots.txt",
    "/.well-known/security.txt",
    "/dashboard.json",
  ])("refuses targets that aren't pages: %s", (next) => {
    expect(safeNextPath(next)).toBe(DEFAULT_AFTER_LOGIN);
  });

  it.each([
    ["/pipeline/0b8f9a9e-1d1e-4c55-9f3a-2f2b5f1e7a10", "/pipeline/0b8f9a9e-1d1e-4c55-9f3a-2f2b5f1e7a10"],
    ["/admin/users/0b8f9a9e-1d1e-4c55-9f3a-2f2b5f1e7a10/pipeline", "/admin/users/0b8f9a9e-1d1e-4c55-9f3a-2f2b5f1e7a10/pipeline"],
    ["/apiary", "/apiary"],
    ["/settings?tab=api", "/settings?tab=api"],
    ["/activities#api", "/activities#api"],
  ])("still keeps ordinary pages, even ones that merely mention the word: %s", (next, expected) => {
    expect(safeNextPath(next)).toBe(expected);
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
    expect(loginPath("/api/v1/workspaces/all", "expired")).toBe("/login?reason=expired");
  });
});
