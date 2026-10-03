/**
 * Where to go after signing in. The `next` value comes from the URL, so it is untrusted:
 * only same-origin, absolute paths are accepted (no open redirects). Anything else falls
 * back to the dashboard.
 *
 * The check runs on the *normalised* path as well as the raw input: URL parsing resolves
 * dot segments, so "/.//evil.com" or "/%2e%2e//evil.com" become "//evil.com" (a
 * protocol-relative URL) only after parsing.
 */
export const DEFAULT_AFTER_LOGIN = "/dashboard";

const PUBLIC_AUTH_PATHS = ["/login", "/forgot-password", "/reset-password", "/activate"];
const PROBE_ORIGIN = "https://arkray.invalid";
// A plain "/" followed by something that is not a slash or backslash; no control
// characters (browsers strip tabs/newlines, turning "/\t/evil.com" into "//evil.com").
const SAFE_PATH = /^\/(?![/\\])[^\\\u0000-\u001f\u007f]*$/;

function isSafePath(path: string): boolean {
  if (!SAFE_PATH.test(path)) return false;
  try {
    return new URL(path, PROBE_ORIGIN).origin === PROBE_ORIGIN;
  } catch {
    return false;
  }
}

export function safeNextPath(next: string | null | undefined): string {
  if (!next || next.length > 2000 || !isSafePath(next)) return DEFAULT_AFTER_LOGIN;
  const url = new URL(next, PROBE_ORIGIN);
  const normalised = `${url.pathname}${url.search}${url.hash}`;
  if (!isSafePath(normalised)) return DEFAULT_AFTER_LOGIN;
  if (PUBLIC_AUTH_PATHS.some((p) => url.pathname === p || url.pathname.startsWith(`${p}/`))) {
    return DEFAULT_AFTER_LOGIN;
  }
  return normalised;
}

export function loginPath(next?: string, reason?: string): string {
  const params = new URLSearchParams();
  const safe = safeNextPath(next);
  if (safe !== DEFAULT_AFTER_LOGIN) params.set("next", safe);
  if (reason) params.set("reason", reason);
  const query = params.toString();
  return query ? `/login?${query}` : "/login";
}
