# 0003. HTTP-only session cookies on a single origin

Status: Accepted
Date: 2026-09-30

## Context
The requirements prefer HTTP-only authentication over tokens readable by JavaScript, and
require CSRF protection, expiry, logout, deactivation and password reset.

## Decision
- Django server-side sessions stored in PostgreSQL. The `arkray_session` cookie is HttpOnly,
  Secure (production) and SameSite=Lax, with a 12 h absolute lifetime and a 2 h idle timeout
  (Phase 1).
- The browser sees **one origin**: a reverse proxy routes `/api` and `/health` to Django and
  everything else to Next.js. In development, Next.js rewrites `/api` to Django. No CORS.
- CSRF: Django's double-submit token (`arkray_csrftoken` cookie → `X-CSRFToken` header) plus
  Origin checking; login is explicitly CSRF-protected.
- The session key is rotated on login; logout flushes; deactivation takes effect on the next
  request.
- The frontend fetches data client-side with `credentials: "same-origin"`; no tokens in JS.

## Consequences
- XSS cannot steal the session; logout and deactivation are immediate (server-side state).
- A Redis outage doesn't log anyone out.
- Server-rendering authenticated data in Next.js would need cookie forwarding; not needed
  here.

## Alternatives considered
- JWT in localStorage: readable by any injected script, and revocation is hard.
- JWT in cookies: all the cookie handling of sessions, plus revocation complexity.
- Cross-origin API + CORS: more configuration and more ways to get it wrong.
