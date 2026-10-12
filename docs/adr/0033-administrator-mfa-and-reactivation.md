# 0033. Two-step verification (TOTP) for administrators; reactivated administrators reset their password

Status: Accepted
Date: 2026-10-11

## Context
The production readiness review left two identity risks open. R125: no account had a second
factor, so one phished administrator password gave organisation-wide reads, privacy exports,
support sessions and user administration. R126: one administrator could reactivate another,
whose old password then worked again, so an account deactivated for cause came back with the
credentials it was deactivated over.

## Decision

### Second factor
- **Standard TOTP** (RFC 6238: HMAC-SHA1, 6 digits, 30-second steps, one step of clock drift
  either way), implemented in `identity.mfa` against the RFC's test vectors; no new runtime
  dependency for the codes. Any authenticator app works (the QR code is an `otpauth://` URI).
- **Available to every user, required for administrators** in production
  (`MFA_REQUIRED_FOR_ADMINS`, true by default there and refused when false unless the local
  HTTP opt-in is set; false by default in development and tests, where dedicated tests turn it
  on).
- **Secrets are encrypted at rest** (Fernet; the key derived from `SECRET_KEY` and its
  fallbacks with a dedicated context, so a key rotation keeps working while the old key is a
  fallback; each successful verification re-encrypts under the current key and
  `manage.py mfa_reencrypt` does all of them before a fallback is retired). The secret is
  shown once, at setup, and never returned again.
- **Enrolment verifies before it activates**: setup (after the current password) creates an
  unconfirmed device and returns the secret, the `otpauth://` URI and the QR code as a module
  matrix (the web app draws it as SVG: no image data URI, no inline HTML); the device counts
  only once a code from it is confirmed. Confirmation returns ten single-use **recovery codes**
  once (80 bits each, shown as `XXXX-XXXX-XXXX-XXXX`, stored as SHA-256 only). A setup expires:
  it must be confirmed within `MFA_SETUP_TTL_S` (10 minutes) and with fewer than five wrong
  codes, each also a failed sign-in in the durable throttle; then it is deleted (also by the
  hourly housekeeping) and starts again with the password (review P2-2).
- **Replay prevention**: the last accepted time step is stored on the device and updated under
  its row lock; a code for that step or an earlier one is refused, also on another server.
- **Sign-in** with a second factor is two requests. `POST /auth/login` checks the password
  exactly as before; for an account with a confirmed device it answers **202**
  `{"mfa_required": true, "expires_in": 300}` and keeps only a pending challenge in a new,
  anonymous session (user id, its session hash, the time, attempts): no signed-in session. `POST
  /auth/login/mfa` with `{"code"}` or `{"recovery_code"}` then signs in (200 with the viewer,
  exactly as `login` did). Five wrong codes void the challenge; every wrong code also counts as
  a failed sign-in in the durable (PostgreSQL) sign-in throttle, so the second factor can't be
  guessed across challenges either. A password change or "sign out everywhere" between the two
  steps voids the challenge. A right password never forgives earlier failures of an account
  with a second factor (not at sign-in, not when re-checked for a sensitive change): only a
  right code does (review P2-1).
- **Enrolment is enforced**: an administrator without a confirmed device (when required)
  reaches only who-am-I, sign-out, the password change and the MFA setup endpoints
  (`allowed_before_mfa_enrollment`), like a forced password change: 403
  `mfa_enrollment_required` elsewhere. The viewer carries `mfa_enabled` and
  `mfa_enrollment_required`.
- **Step-up**: the session records when a factor was last verified. Views marked
  `requires_recent_mfa` (user administration changes, starting a support session, requesting
  a privacy export, pseudonymisation) answer 403 `mfa_required` when the
  caller has a confirmed device and their last verification is older than
  `MFA_STEP_UP_TTL_S` (15 minutes); `POST /auth/mfa/verify` with a code renews it, and the web
  app asks for the code and repeats the request once. Erasure and legal holds have no API
  (operator commands, behind shell access). An export's download is an ordinary link for its
  requester only and doesn't ask again (a 403 there would end on a JSON page).
- **Support sessions don't bypass it**: starting one needs a recent verification; inside one,
  every MFA endpoint is refused (`refused_in_support_session`), and the administrator's own
  session is the one that was verified.
- **Changes end other sessions**: confirming, disabling or resetting a device and replacing
  recovery codes increment `session_epoch` (this session is kept, re-hashed). The browser making
  the change gets its trusted-device cookie re-issued (as a password change or one's own email
  change now does too), so it keeps its own failure budget and an attacker who knows only the
  email can't lock its owner out of it (review P2-3).
- **Recovery and reset**: a recovery code signs in (and is used up; the security events show
  it). A user may disable their own device with their password and a code, unless it is
  required for their role. An administrator may reset an **ordinary user's** device (audited,
  ends their sessions). No administrator can reset another administrator's device (ADR-0030):
  an operator runs `manage.py reset_mfa --email ... --by <administrator> --reason ...` on the
  server (audited with that administrator as the actor; the sole administrator may name
  themselves).
- **Audit**: `mfa.enabled`, `mfa.disabled`, `mfa.reset`, `mfa.recovery_codes_replaced`,
  `mfa.recovery_code_used` (security events), and `auth.login` records the factor used; never
  a secret, a code or a hash. Each of these changes also **emails the account's own address**
  (no code, no secret), so an owner learns of an enrolment they didn't make (for example an
  attacker with a phished password enrolling first during the rollout) or of a reset.
- **Failed sign-ins** of existing accounts are audited (`auth.login_failed`, shown in security
  events; bounded by the durable throttle per account), not only at the lockout threshold.
  Unknown emails stay a log line (no audit rows an anonymous caller can create at will).

### Decisions taken consciously (adversarial review)
- **Step-up scope** excludes reassigning records (`crm.assign_any`), Ask Arkray, ending a
  support session, pipeline and field configuration, and export downloads: they expose or
  change nothing an administrator's session can't already read or undo, and asking for a code
  on every routine action would train people to type codes anywhere. Revisit if one of them
  becomes a way to move data out.
- **A step-up survives a password change in the same session** (the session that changed the
  password is the one that proved the code; other sessions end).
- **An administrator can make an account they control an administrator** (reset an ordinary
  user's second factor, set a temporary password, promote the account): not a privilege
  gain, since an administrator can already invite a new administrator at any address; every
  step is audited, and the reset, the password set and the promotion all appear in the
  security events (and the owner is emailed about the reset). Accepted.

### Reactivating an administrator (R126)
When an administrator reactivates **another administrator**, the account comes back active
with **no known password** (a random one nobody sees replaces it: the database requires an
active account to hold a usable hash, `identity_user_active_has_password`), a new
`session_epoch`, its pending reset links revoked, and a
password-reset link mailed to the account's own address. The old password never works again;
the second factor (if any) is kept, so the owner still needs it to sign in. Reactivating an
ordinary user is unchanged (their own password), as ADR-0030 documents. Deactivating an
administrator is shown in security events and raises the `ArkrayAdministratorDeactivated`
alert.

## Consequences
- One phished administrator password is no longer enough to sign in, administer users, start
  support sessions or export personal data.
- An administrator who loses both the authenticator and the recovery codes needs an operator
  (documented in the runbooks), never another administrator.
- Production refuses to start with `MFA_REQUIRED_FOR_ADMINS=false` (local opt-in aside); after
  the upgrade, each administrator enrols at their next sign-in.
- WebAuthn/passkeys remain a possible later addition; neither the session design nor the
  capability model needs to change for it.

## Addendum (deep security audit M-4, 2026-10-12): wrong codes accumulate until the factor locks

The durable sign-in throttle only slows guessing down: its window slides, so someone with the
password could try about 550 codes a day for as long as they liked (about a 5 % chance in 30
days), and the owner was never told (R133). Now:

- Every wrong code (authenticator or recovery code; at sign-in, step-up, replacing recovery
  codes, disabling) increments `MfaDevice.consecutive_failures` under the device's row lock,
  across sign-in attempts, browsers and sessions; a right code resets it to 0.
- From `MFA_MAX_CONSECUTIVE_FAILURES` (20) on, **no code is accepted, the right one
  included**: `403 mfa_locked` (the sign-in challenge ends; the web app goes back to the
  password step with the explanation). Each guess matches at most 3 of 10^6 codes (one step of
  drift either way), so 20 guesses leave a password thief about a 6 in 100,000 chance, for the
  life of that password. 20 is four whole challenges (five codes each), comfortably beyond an
  owner's typos (a right code in between resets the count), and well under NIST SP 800-63B's
  ceiling of 100 consecutive failures. The audit suggested 50 (1.5 in 10,000); 20 is stricter
  at no real cost.
- **Unlocking** needs something a password thief doesn't have: a password reset through the
  emailed link (`auth.password_reset_completed`), a password set by an administrator
  (ordinary users only, as before), or an MFA reset (administrator for ordinary users,
  `manage.py reset_mfa` for anyone; the device and its count are deleted). The first two
  record `mfa.unlocked` (`by`: `password_reset` / `administrator`). A password change in a
  signed-in session does **not** unlock: that session may be the thief's.
- **Not a lever against owners whose password is safe**: the count only grows behind a
  right password (a pending challenge) or a signed-in session (step-up), so nobody without
  one of those can lock anyone out. Someone who has the password can lock the factor on
  purpose; the owner then has to reset the password, which is exactly what they should do
  when their password is known to someone else.
- **The owner is told**: the first wrong code in `MFA_FAILURE_NOTICE_INTERVAL_S` (6 hours)
  emails "A wrong verification code was entered" (a code is asked for only after the right
  password, so if it wasn't them, reset the password); locking emails "Two-step verification
  is locked". Neither contains a code. Outbox, in the transaction that counts the failure.
- **Operators are told**: `mfa.locked` (ids and the count only) is audited and shown in the
  security events; `arkray_mfa_lockouts_last_hour` raises `ArkrayMfaLocked` (ticket;
  [runbooks](../runbooks.md#second-factor-locked)).
- Migration `identity.0006_mfa_consecutive_failures` adds `consecutive_failures`
  (`db_default=0`: the previous release still inserts) and `failure_notified_at`; it refuses to
  reverse, like every post-RC migration.
