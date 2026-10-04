# 0024. Security hardening: sealed and bound page links, prefixed cookies, a nonce CSP, audit outside the cache

Status: Accepted
Date: 2026-10-04

## Context
Phase 9 audited the whole application (Phases 0–8) with four independent adversarial
reviews (authentication and sessions, authorization, injection and secrets, Ask Arkray)
and closed the hardening items earlier phases had deferred. Several fixes changed how a
mechanism works rather than a line of code, and are recorded here:

1. **Keyset cursors** were signed but readable, bound only to their ordering, and never
   expired. A cursor could be replayed by another user, in another workspace or with other
   filters (never widening access: the queryset is scoped first, and private sort values
   are re-read through the caller's scope since Phase 6), and its sort values and row ids
   were readable (R48: a global sequence id counted organisation-wide events).
2. **Cookies** had plain names, so a sibling subdomain could set or shadow them.
3. **Pages** had no Content-Security-Policy.
4. **The audit window** for delegated viewing was a cache key: a write to Redis could hide
   an administrator's access indefinitely; a Redis outage audited every request.
5. **The cache** used django-redis's default pickle serializer: write access to Redis was
   code execution in every process reading the cache.

## Decision
- **Cursors are sealed and bound.** A cursor is the payload encrypted and authenticated with
  Fernet (`cryptography`), keyed from `SECRET_KEY` and its fallbacks; it carries an HMAC of
  what it may continue: the list (its purpose, including the record whose timeline or
  history it pages), the signed-in user, the workspace (as a scope, so `me` and one's own id
  are the same) and the validated filters (empty values are no filter; the page size is not
  bound). It expires with the longest session (`KEYSET_CURSOR_MAX_AGE_S` =
  `SESSION_COOKIE_AGE`). Every list passes a binding (an architecture test refuses `None`
  in application code); the board binds each column's cursor exactly as the opportunities
  list will check it; the user table moved from DRF's cursor to the same paginator.
  Append-only rows (timeline entries, stage history) show opaque ids (a keyed hash).
- **Cookies.** Over HTTPS the session and CSRF cookies are `__Host-`; the trusted-browser
  cookie keeps a path (now `/api/v1/`, so the own-email re-authentication sees it) and is
  `__Secure-`; its value is also bound to the account's credentials (a reset retires every
  trusted browser). A plain-HTTP local stack keeps the plain names. One host serves the
  pages and the API.
- **CSP.** A per-request nonce from Next.js's proxy (`script-src 'self' 'nonce-…'
  'strict-dynamic'`, nonce-gated styles, no `unsafe-inline`, no `eval` in production); every
  page renders per request so the nonce reaches Next.js's own scripts. The API's JSON
  carries `default-src 'none'`.
- **Audit window in PostgreSQL.** A row per actor, workspace and 15-minute window, inserted
  with `ON CONFLICT DO NOTHING` in the audit row's transaction (one read per request once
  open).
- **The cache never unpickles** (JSON serializer); production refuses Redis URLs without a
  password.
- **Writes are authorised before the body is read**, in the views as well as the services.

## Consequences
- A page link from before the deploy, from another user, workspace, record, list or filter
  set, or older than a session, is a 400 ("This page link is invalid or has expired"); the
  lists already offer to start over. Rotating `SECRET_KEY` keeps links working (fallbacks).
- Renaming the cookies signs everyone out once, at the first HTTPS deploy with them; moving
  the API to a sibling host would break sign-in by design.
- Every page is server-rendered per request (no static prerendering): measured negligible
  for this app, whose pages are client components.
- One more query (a read) per delegated or organisation-wide request; the query-count tests
  were updated.
- A new runtime dependency, `cryptography` (Fernet).

## Alternatives considered
- **Signed cursors with the binding only** (no encryption): leaves R48's readable ids;
  Python's standard library has no authenticated cipher, and a home-made one is worse than
  the dependency.
- **Opaque server-side cursor ids** (a table of page positions): exact and short, but a
  write per page and housekeeping, for no gain over a sealed cursor.
- **`style-src 'unsafe-inline'`**: unnecessary; the live sweep found no style violations.
- **Keeping the audit window in Redis with a fail-closed outage mode**: still trusts what
  Redis says when it is up; a database row costs one indexed read.
