/**
 * Where to go after signing in. The `next` value comes from the URL, so it is untrusted:
 * only same-origin, absolute paths are accepted (no open redirects). Anything else falls
 * back to the dashboard.
 *
 * The check runs on the *normalised* path as well as the raw input: URL parsing resolves
 * dot segments, so "/.//evil.com" or "/%2e%2e//evil.com" become "//evil.com" (a
 * protocol-relative URL) only after parsing.
 *
 * Only a page of this app is a target (adversarial review of 73ff1e3, L9): a link to
 * `/login?next=/api/v1/workspaces/<id>` made a signed-in administrator's browser send a
 * same-origin GET to the API, past the cross-site read guard, writing `workspace.accessed` in
 * their name. A page path is lower-case words, digits and hyphens between single slashes (no
 * dots, percent-encoding, upper case or other punctuation, which the edge or the server could
 * decode or fold into `/api/...`), and its first segment isn't one the edge routes elsewhere
 * (`/api`, `/health`; `/_next` and files such as `/icon.svg` don't have the shape at all).
 */
export const DEFAULT_AFTER_LOGIN = "/dashboard";

const PUBLIC_AUTH_PATHS = ["/login", "/forgot-password", "/reset-password", "/activate", "/unlock-sign-in"];
const PROBE_ORIGIN = "https://arkray.invalid";
// A plain "/" followed by something that is not a slash or backslash; no control
// characters (browsers strip tabs/newlines, turning "/\t/evil.com" into "//evil.com").
const SAFE_PATH = /^\/(?![/\\])[^\\\u0000-\u001f\u007f]*$/;

// The shape of a page's path (the query and the fragment are free).
const PAGE_PATH = /^\/(?:[a-z0-9][a-z0-9-]*(?:\/[a-z0-9][a-z0-9-]*)*\/?)?$/;
// First segments the edge proxy sends somewhere other than the web app's pages.
const NOT_PAGES = new Set(["api", "health"]);

function isPage(pathname: string): boolean {
  if (!PAGE_PATH.test(pathname)) return false;
  return !NOT_PAGES.has(pathname.split("/")[1] ?? "");
}

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
  if (!isSafePath(normalised) || !isPage(url.pathname)) return DEFAULT_AFTER_LOGIN;
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
