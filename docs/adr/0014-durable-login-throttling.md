# 0014. Durable login throttling with per-browser budgets

Status: Accepted
Date: 2026-09-30

## Context
Sign-in must resist brute force against one account, password spraying across many
accounts, and floods from one source. It must keep working when Redis is down, must not
reveal whether an account exists, and must not let an attacker lock a real user out
permanently just by sending wrong passwords. Classic per-account lockout fails that last
requirement: whoever can send five bad passwords can keep the victim locked out forever.

## Decision
- Throttling evidence lives in PostgreSQL (`identity_auth_throttle_event`), not Redis.
  DRF's Redis-backed rate limits remain as a first, fail-open layer.
- **Key:** a keyed HMAC of the *submitted*, normalised email, so unknown emails are
  throttled exactly like real ones. No email address is stored.
- **Per account and browser:** a browser that has signed in to the account before carries
  a signed, HttpOnly, `SameSite=Strict` trusted-device cookie scoped to `/api/v1/auth/`.
  Such a browser gets its own failure budget, and all unrecognised browsers share one.
  Five failures in 15 minutes lock that bucket for 1 minute, doubling with each further
  failure up to 15 minutes. Attackers exhaust only their own bucket, and the owner's usual
  browser keeps working.
- **Per source** (trusted-proxy-aware client IP; IPv6 counted per /64; unknown addresses
  share one bucket): 50 failures in 15 minutes block unrecognised browsers from that
  source, which catches spraying. Trusted browsers are exempt, so a neighbour behind the
  same NAT can't lock the owner out.
- Throttled attempts are refused **before** the password is checked and are not recorded,
  so there is no oracle and the evidence can't grow without bound. Every read uses a partial
  index with a LIMIT.
- **Atomic reservations:** an attempt reserves a failure in a short transaction (advisory
  locks on the account and the source, a count, an insert) before the password is checked
  outside any transaction; a correct password deletes the reservation. Parallel bursts
  can't exceed a budget, and the owner is never refused because an attacker's attempt is
  in flight. (The first version used a non-blocking per-account lock that answered
  "busy"; the Phase 1 review showed an attacker could use it to block the owner.)
- Keys honour `SECRET_KEY_FALLBACKS`, so rotation keeps lockouts and trusted browsers.
- Timing is equalised: unknown or password-less accounts verify a decoy hash.
- Crossing a threshold is audited once (`auth.login_throttled`), without the email.
- A successful sign-in clears that browser's bucket. A completed password reset or
  activation clears the account's buckets, because the owner has proved control of the
  mailbox.
- Evidence older than 24 hours is purged hourly.

## Consequences
- Brute-force protection survives a Redis outage (tested).
- A distributed attacker can still keep *unrecognised* browsers throttled for an account
  (at most 15 minutes at a time). The owner's trusted browsers and the password-reset path
  are unaffected. Offices behind one NAT share the 50-failure source budget.
- The response to a throttled attempt is `429` with `Retry-After`, identical for existing
  and non-existing accounts.

## Alternatives considered
- **Redis-only rate limiting:** fails open during an outage.
- **Hard per-account lockout:** a denial-of-service lever for attackers.
- **CAPTCHA after N failures:** needs a third-party service, and hurts accessibility. It
  can be layered on later.
