// @vitest-environment node
import { NextRequest } from "next/server";
import { describe, expect, it } from "vitest";

import { config, contentSecurityPolicy, proxy } from "./proxy";

function directives(policy: string): Map<string, string> {
  return new Map(
    policy.split(";").map((part) => {
      const [name, ...values] = part.trim().split(/\s+/);
      return [name!, values.join(" ")];
    }),
  );
}

describe("Content-Security-Policy (Phase 9)", () => {
  it("allows scripts and styles only from this origin or with the page's nonce", () => {
    const policy = directives(contentSecurityPolicy("abc123", false));
    expect(policy.get("script-src")).toBe("'self' 'nonce-abc123' 'strict-dynamic'");
    expect(policy.get("style-src")).toBe("'self' 'nonce-abc123'");
    expect(policy.get("default-src")).toBe("'self'");
    expect(policy.get("connect-src")).toBe("'self'");
    expect(policy.get("object-src")).toBe("'none'");
    expect(policy.get("base-uri")).toBe("'self'");
    expect(policy.get("form-action")).toBe("'self'");
    expect(policy.get("frame-ancestors")).toBe("'none'");
  });

  it("never allows inline code or eval in production", () => {
    const policy = contentSecurityPolicy("abc123", false);
    expect(policy).not.toContain("unsafe-inline");
    expect(policy).not.toContain("unsafe-eval");
    expect(policy).not.toContain("*");
    expect(contentSecurityPolicy("abc123", true)).toContain("'unsafe-eval'"); // dev tooling only
  });

  it("puts a fresh nonce on every page request and the same policy on the response", () => {
    const first = proxy(new NextRequest("http://localhost:3000/leads"));
    const second = proxy(new NextRequest("http://localhost:3000/leads"));
    const nonceOf = (policy: string | null) => /'nonce-([^']+)'/.exec(policy ?? "")?.[1];
    const a = nonceOf(first.headers.get("Content-Security-Policy"));
    const b = nonceOf(second.headers.get("Content-Security-Policy"));
    expect(a).toBeTruthy();
    expect(b).toBeTruthy();
    expect(a).not.toBe(b);
    // Next.js reads the nonce from the request it renders: the policy is forwarded too.
    expect(first.headers.get("x-middleware-request-content-security-policy")).toBe(
      first.headers.get("Content-Security-Policy"),
    );
    expect(first.headers.get("x-middleware-request-x-nonce")).toBe(a);
  });

  it("runs for pages, not for the API, static chunks or the icon", () => {
    const source = new RegExp(`^${config.matcher[0]!.source}$`);
    for (const page of ["/", "/login", "/leads/1", "/admin/users/1/ask", "/reset-password/t"]) {
      expect(source.test(page)).toBe(true);
    }
    for (const asset of ["/api/v1/auth/me", "/_next/static/chunks/a.js", "/_next/image", "/icon.svg"]) {
      expect(source.test(asset)).toBe(false);
    }
  });
});
