/**
 * TanStack Query setup (docs/reliability.md "Retries"):
 * - GET queries retry twice with backoff, but never on 4xx (retrying cannot fix those),
 *   never after a timeout (the request already waited 15 s: retries made a hung backend
 *   look like 49 s of loading), and only when the server's Retry-After is short; they wait
 *   what Retry-After asks (whole-software audit);
 * - mutations are never retried automatically;
 * - any 401 on a signed-in page means the session ended (idle timeout, deactivation,
 *   password change elsewhere): the app reloads onto the sign-in page, dropping all cached
 *   data, and comes back here afterwards.
 */
import { MutationCache, QueryCache, QueryClient } from "@tanstack/react-query";

import { ApiError } from "./api/client";
import { currentLocation, hardNavigate } from "./browser";
import { loginPath } from "./safe-redirect";

export const VIEWER_QUERY_KEY = ["viewer"] as const;

const PUBLIC_PREFIXES = ["/login", "/forgot-password", "/reset-password", "/activate"];

export function isPublicPath(pathname: string): boolean {
  return PUBLIC_PREFIXES.some((p) => pathname === p || pathname.startsWith(`${p}/`));
}

/** A server asking to be left alone longer than this isn't retried automatically. */
const MAX_RETRY_AFTER_S = 4;

export function shouldRetry(failureCount: number, error: unknown): boolean {
  if (error instanceof ApiError) {
    if (error.status >= 400 && error.status < 500) return false;
    if (error.code === "timeout") return false;
    if (error.retryAfterSeconds !== null && error.retryAfterSeconds > MAX_RETRY_AFTER_S) return false;
  }
  return failureCount < 2;
}

export function retryDelay(failureCount: number, error: unknown): number {
  if (error instanceof ApiError && error.retryAfterSeconds !== null) return error.retryAfterSeconds * 1000;
  return Math.min(1000 * 2 ** failureCount, 30_000);
}

export function createQueryClient(): QueryClient {
  let redirecting = false;
  const client: QueryClient = new QueryClient({
    queryCache: new QueryCache({ onError: (error) => onError(error) }),
    mutationCache: new MutationCache({ onError: (error) => onError(error) }),
    defaultOptions: {
      queries: { retry: shouldRetry, retryDelay, staleTime: 30_000, refetchOnWindowFocus: false },
      mutations: { retry: false },
    },
  });

  function onError(error: unknown): void {
    if (!(error instanceof ApiError) || error.status !== 401 || redirecting) return;
    if (isPublicPath(window.location.pathname)) return;
    redirecting = true;
    const hadSession = client.getQueryData(VIEWER_QUERY_KEY) !== undefined;
    hardNavigate(loginPath(currentLocation(), hadSession ? "expired" : undefined), hadSession ? "signed-out" : undefined);
  }

  return client;
}
