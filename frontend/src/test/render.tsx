import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { ReactElement, ReactNode } from "react";
import { vi } from "vitest";

import type { Viewer } from "@/lib/viewer";
import { ViewerProvider } from "@/lib/viewer-context";

export function createTestQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: Infinity }, mutations: { retry: false } },
  });
}

export function renderWithProviders(
  ui: ReactElement,
  { viewer = null, client = createTestQueryClient() }: { viewer?: Viewer | null; client?: QueryClient } = {},
) {
  // A wrapper (not part of `ui`), so rerender() keeps the providers and the cache.
  function Providers({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={client}>
        <ViewerProvider viewer={viewer}>{children}</ViewerProvider>
      </QueryClientProvider>
    );
  }
  return { client, ...render(ui, { wrapper: Providers }) };
}

// --- a tiny API mock: routes keyed "METHOD /path" -----------------------------------------
export interface RecordedCall {
  method: string;
  path: string;
  query: URLSearchParams;
  body: unknown;
  headers: Record<string, string>;
}

type Reply = { status: number; body?: unknown; headers?: Record<string, string> };
type Route = Reply | ((call: RecordedCall) => Reply | Promise<Reply>);

export function json(status: number, body?: unknown, headers: Record<string, string> = {}): Response {
  return new Response(body === undefined ? null : JSON.stringify(body), {
    status,
    headers: body === undefined ? headers : { "Content-Type": "application/json", ...headers },
  });
}

export function apiError(status: number, code: string, message: string, details: unknown = null): Reply {
  return { status, body: { error: { code, message, details, request_id: "req-test-1" } } };
}

export function mockApi(routes: Record<string, Route>) {
  const calls: RecordedCall[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://localhost");
    const method = (init?.method ?? "GET").toUpperCase();
    const call: RecordedCall = {
      method,
      path: url.pathname,
      query: url.searchParams,
      body: typeof init?.body === "string" ? JSON.parse(init.body) : undefined,
      headers: (init?.headers ?? {}) as Record<string, string>,
    };
    if (url.pathname === "/api/v1/auth/csrf") {
      document.cookie = "arkray_csrftoken=test-csrf-token; path=/";
      return json(204);
    }
    calls.push(call);
    const route = routes[`${method} ${url.pathname}`];
    if (!route) return json(404, { error: { code: "not_found", message: "Not found.", details: null, request_id: "req-404" } });
    const reply = typeof route === "function" ? await route(call) : route;
    return json(reply.status, reply.body, reply.headers);
  });
  vi.stubGlobal("fetch", fetchMock);
  return { fetchMock, calls, callsTo: (method: string, path: string) => calls.filter((c) => c.method === method && c.path === path) };
}
