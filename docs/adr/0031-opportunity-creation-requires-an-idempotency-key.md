# 0031. Opportunity creation requires an Idempotency-Key

Status: Accepted
Date: 2026-10-06

## Context
A new opportunity creates its lead in the same transaction (ADR-0028), so a duplicated request
makes two opportunities **and** two leads. The `Idempotency-Key` header was optional: the UI always
sent one, but any other API client, a retrying proxy or a script that did not could create
duplicates (final audit DOMAIN-1, risk R103). A uniqueness constraint on customer, account or
instrument names is not an answer: legitimate duplicate opportunities exist, and customers must
never be merged silently.

## Decision
- `POST /workspaces/{workspace}/opportunities` **requires** the header (a UUID). Missing or
  malformed is a 400 `validation_error` naming `idempotency_key`, and nothing is written.
- Concurrent requests with one key serialise on a transaction-level advisory lock taken first
  (actor, operation, key), re-check the record and replay the first result (`Idempotent-Replayed:
  true`); the unique index stays as the backstop. A key reused for a different request is a 422.
  Records last 24 hours.
- A new key with identical details is a new opportunity: the key is the identity of the *logical
  submission*. There is deliberately no content-based duplicate guard.
- The UI generates the key with Web Crypto only (no `Math.random` fallback), keeps one key per
  workspace-and-body, reuses it for a retry of the same details, and creates a new one for a new
  opportunity and after a refused key. The API client refuses to send an opportunity create
  without one. The key is not kept in the browser: a reload starts a new submission.
- Other creates (lead, activity, lead conversion) keep the optional header; conversion cannot
  convert twice anyway. [api-conventions.md](../api-conventions.md) lists which require it.

## Consequences
- Duplicate logical creates are impossible under the supported request contract (2, 20 and 100
  parallel requests tested at database level and live through the proxy).
- API clients written against the old optional contract get a clear 400 until they send a key.
- A person who loses the response, reloads the page and retypes the same deal creates a second
  one: their choice, visible on the board.

## Alternatives considered
- **Server-generated dedupe window on (user, content)**: breaks legitimate duplicates.
- **Keeping the key in sessionStorage**: would cover reload-and-resubmit, at the cost of keys
  outliving their form; not needed for the supported contract.
