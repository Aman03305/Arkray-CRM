# 0013. Explicit account lifecycle and stored, hashed one-time account tokens

Status: Accepted
Date: 2026-09-30

## Context
Phase 0 designed invitations and password resets around Django's stateless HMAC token
generator (a token derived from the user id, password hash and an `invited_at` stamp) and a
status derived from timestamps. Phase 1 requires invitation and reset credentials that are
random, high-entropy, single-use, expiring, safe against a database leak, with lifecycle
integrity enforced by the database and correct behaviour under races (two activations of
one link, a resend racing an activation, two confirmations of one reset link).

Stateless tokens can't meet all of that. Two confirmations of the same HMAC token can both
verify before either commits, supersession is only implicit, nothing records which link was
issued or used, and every outstanding link dies when `SECRET_KEY` rotates. Phase 0 also
exposed a gap in deactivation: Django rejects an inactive user's session only while the
user stays inactive, so reactivating them would revive every old session, such as one on a
stolen laptop.

## Decision
- **Explicit lifecycle** on `identity_user`: `status` is `invited`, `active` or
  `deactivated`, with `activated_at` and `deactivated_at`. `is_active` stays as a column,
  because Django's auth backend and DRF check it on every request, and a CHECK constraint
  pins it to `status`. Further CHECKs ensure that an active user has a usable password, an
  invited user has never activated, and a deactivation date exists exactly for deactivated
  users. `first_name` and `last_name` replace `full_name`, which is now computed.
- **`identity_account_token`** holds invitations and password resets (`purpose`), with
  `status` of `pending`, `used` or `revoked`, plus `expires_at`, `issued_at`, `sent_at`,
  `used_at` and `revoked_at`. A partial unique index allows **at most one pending token per
  user and purpose**, so supersession is enforced by the database. CHECKs tie each
  timestamp to its status.
- **Only a digest is stored.** The secret is 32 bytes from the OS CSPRNG (43 URL-safe
  characters), and only its SHA-256 is stored. A slow hash would add nothing, because
  256 bits of entropy can't be brute-forced.
- **The secret is minted by the email job, not the web request.** The request creates the
  token row, with no secret yet, and an outbox event in the same transaction. The worker
  mints the secret, stores its digest, commits and sends the email. So the plaintext
  exists only in that worker's memory and in the email, never in a web request, the outbox
  table or logs. A failed send retries through the outbox and mints a fresh secret, which
  makes the undelivered one worthless. There is still only one retry layer.
- **Consumption locks the user row, then the token row**, always in that order, and
  re-validates the digest, status, expiry and account state under the locks.
- **Password-reset requests do constant work.** The API records the request and enqueues
  a job whether or not the account exists. The job decides eligibility (active accounts
  only, at most 3 per hour), so neither the response nor its timing reveals anything.
- **`session_epoch`** is folded into the session auth hash, overriding
  `User._get_session_auth_hash`. Deactivation and email changes increment it, so every
  existing session ends permanently, even across a later reactivation. Password changes
  already change the hash.
- User administration writes are serialised by one transaction-scoped advisory lock, and
  the acting administrator is re-read under it.

## Consequences
- Link lifecycle is auditable and queryable. The admin UI shows "sending", "sent" and
  "expired" invitation states from real data.
- Races resolve deterministically, and the concurrency tests prove it.
- Rotating `SECRET_KEY` no longer voids outstanding links. It still ends sessions.
- If the worker crashes after sending and before recording `sent_at`, a retry sends a
  second email whose link supersedes the first. This is the at-least-once trade-off, and it
  is harmless.
- One more table and one more outbox hop for password resets (request → issue → deliver).

## Alternatives considered
- **Django's `PasswordResetTokenGenerator`:** not random, not single-use under
  concurrency, not stored, and tied to `SECRET_KEY`.
- **Storing the secret encrypted in the outbox payload:** adds key management, and a
  database leak together with that key exposes live links.
- **A status derived from timestamps only:** workable, but "valid account state" becomes
  harder to express as constraints and to filter by.
- **Deleting sessions on deactivation:** would need a user→session index. The epoch in the
  auth hash invalidates every session with no bookkeeping.
