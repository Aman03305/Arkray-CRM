/**
 * Full page loads, used whenever the signed-in identity changes (sign-in, sign-out, session
 * end). Three layers keep one user's data from ever reaching the next person on the same
 * browser:
 *
 * 1. Before leaving, the app renders nothing (LEAVING_EVENT, handled in app/providers), so
 *    the page kept in the back/forward cache holds no data.
 * 2. A page restored from that cache reloads itself (`pageshow` with `persisted`), and
 *    authenticated pages are sent with `Cache-Control: no-store` (next.config.ts).
 * 3. Other tabs are told (BroadcastChannel) and reload, so a sign-out or a different
 *    sign-in in one tab never leaves another tab showing the previous user's data.
 */
export const LEAVING_EVENT = "arkray:leaving";
export const AUTH_CHANNEL = "arkray-auth";
export type AuthChange = "signed-in" | "signed-out";

export function announceAuthChange(change: AuthChange): void {
  try {
    const channel = new BroadcastChannel(AUTH_CHANNEL);
    channel.postMessage(change);
    channel.close();
  } catch {
    // Old browsers without BroadcastChannel: other tabs catch up on their next request.
  }
}

export function hardNavigate(url: string, change?: AuthChange): void {
  window.dispatchEvent(new Event(LEAVING_EVENT));
  if (change) announceAuthChange(change);
  window.location.assign(url);
}

export function reloadPage(): void {
  window.location.reload();
}

export function currentLocation(): string {
  return `${window.location.pathname}${window.location.search}`;
}
