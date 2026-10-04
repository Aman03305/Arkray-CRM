import { NextResponse, type NextRequest } from "next/server";

/**
 * Content-Security-Policy with a fresh nonce per page request (Phase 9). Next.js reads the
 * nonce from the request's CSP header while rendering and puts it on its own scripts and
 * styles; anything injected without it — a script smuggled into a record's text, an inline
 * handler — does not run. `'strict-dynamic'` lets those trusted scripts load the app's
 * chunks. Pages are therefore rendered per request (the root layout opts in).
 *
 * No `upgrade-insecure-requests`: production is HTTPS with HSTS anyway, and the directive
 * would break the plain-HTTP local stack. API responses (`/api`) are JSON and get the
 * backend's own headers.
 */
export function contentSecurityPolicy(nonce: string, development: boolean): string {
  return [
    "default-src 'self'",
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'${development ? " 'unsafe-eval'" : ""}`,
    `style-src 'self' 'nonce-${nonce}'`,
    "img-src 'self' blob: data:",
    "font-src 'self'",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
  ].join("; ");
}

export function proxy(request: NextRequest): NextResponse {
  const nonce = btoa(crypto.randomUUID());
  const policy = contentSecurityPolicy(nonce, process.env.NODE_ENV === "development");
  const headers = new Headers(request.headers);
  headers.set("x-nonce", nonce);
  headers.set("Content-Security-Policy", policy);
  const response = NextResponse.next({ request: { headers } });
  response.headers.set("Content-Security-Policy", policy);
  return response;
}

export const config = {
  matcher: [
    {
      // Pages only: not the API (proxied to Django), hashed static assets or the icon.
      source: "/((?!api/|_next/static|_next/image|icon.svg).*)",
      missing: [
        { type: "header", key: "next-router-prefetch" },
        { type: "header", key: "purpose", value: "prefetch" },
      ],
    },
  ],
};
