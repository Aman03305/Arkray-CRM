import type { NextConfig } from "next";

// Where /api/* is proxied. Rewrites are resolved at build time, so container builds pass
// this as a build argument. In production a reverse proxy normally routes /api to Django
// directly and this rewrite is never hit; either way the browser only ever talks to one
// origin, so session cookies stay first-party and no CORS is needed.
const apiOrigin = process.env.API_ORIGIN ?? "http://localhost:8000";

const securityHeaders = [
  { key: "X-Frame-Options", value: "DENY" },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "Referrer-Policy", value: "same-origin" },
  { key: "Cross-Origin-Opener-Policy", value: "same-origin" },
  { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=(), payment=()" },
  // The Content-Security-Policy is set per request with a nonce (src/proxy.ts).
];

// Pages whose URL carries a one-time secret (emailed invitation and reset links): never
// send it onwards in a Referer header, and never let any cache store the page.
const secretLinkHeaders = [
  { key: "Referrer-Policy", value: "no-referrer" },
  { key: "Cache-Control", value: "no-store" },
];

const nextConfig: NextConfig = {
  output: "standalone",
  poweredByHeader: false,
  experimental: {
    // Pages are rendered per request only for the CSP nonce; what the server renders depends
    // on the URL alone (no cookies, no data: every record is fetched by the browser). So a
    // page visited in the last 5 minutes opens again from the router's cache, without a
    // round trip. The default (0 s) re-requested every page on every click.
    staleTimes: { dynamic: 300 },
  },
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${apiOrigin}/api/:path*` }];
  },
  // There is no Leads module (ADR-0027): old links to the Leads list, the lead form and lead
  // edit pages open the same workspace's Pipeline. A lead's own page (/leads/{id}) exists
  // again, read-only (ADR-0028), so only those other paths redirect.
  async redirects() {
    const old = ["", "/new", "/:leadId/:rest+"];
    return old.flatMap((path) => [
      { source: `/leads${path}`, destination: "/pipeline", permanent: false },
      { source: `/admin/users/:userId/leads${path}`, destination: "/admin/users/:userId/pipeline", permanent: false },
    ]);
  },
  async headers() {
    return [
      { source: "/:path*", headers: securityHeaders },
      // Pages show personal data once loaded: never store them in any cache (including the
      // browser's back/forward cache after sign-out). Hashed static assets stay cacheable.
      { source: "/((?!_next/static|_next/image|icon.svg).*)", headers: [{ key: "Cache-Control", value: "no-store" }] },
      { source: "/activate/:token*", headers: secretLinkHeaders },
      { source: "/reset-password/:token*", headers: secretLinkHeaders },
    ];
  },
};

export default nextConfig;
