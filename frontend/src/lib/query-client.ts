/**
 * TanStack Query setup (docs/reliability.md "Retries"):
 * - GET queries retry twice with backoff, but never on 4xx (retrying cannot fix those);
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

export function shouldRetry(failureCount: number, error: unknown): boolean {
  if (error instanceof ApiError && error.status >= 400 && error.status < 500) return false;
  return failureCount < 2;
}

export function createQueryClient(): QueryClient {
  let redirecting = false;
  const client: QueryClient = new QueryClient({
    queryCache: new QueryCache({ onError: (error) => onError(error) }),
    mutationCache: new MutationCache({ onError: (error) => onError(error) }),
    defaultOptions: {
      queries: { retry: shouldRetry, staleTime: 30_000, refetchOnWindowFocus: false },
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
