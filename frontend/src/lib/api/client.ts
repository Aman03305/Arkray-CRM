/**
 * The only way the frontend talks to the backend.
 *
 * - Same-origin `/api/*` only: the session cookie is HTTP-only and never readable by JS, and
 *   no request can accidentally be sent to another host.
 * - Unsafe methods echo the CSRF cookie in `X-CSRFToken` (Django's double-submit check). If
 *   the cookie is missing it is fetched first, and a request refused with `csrf_failed`
 *   (for example after sign-in rotated the token) is retried once with a fresh token. That
 *   retry is safe: the server rejected the request before doing anything.
 * - Every request has a timeout, so a hung backend surfaces as an error, not a frozen UI.
 * - Errors are normalised to `ApiError` from the backend envelope
 *   `{"error": {"code", "message", "details", "request_id"}}`.
 * - JSON bodies go through `apiFetch`; a file goes through `apiUpload` (same rules).
 * - A create that requires an Idempotency-Key is never sent without a valid one.
 */
import { isIdempotencyKey } from "@/lib/random";

/**
 * Over HTTPS the backend names its CSRF cookie `__Host-arkray_csrftoken` (Secure, host-only,
 * Path=/: a sibling subdomain can't set or shadow it); a plain-HTTP local stack can't use the
 * prefix and keeps `arkray_csrftoken`. The prefixed cookie wins when both exist.
 */
export const CSRF_COOKIE_NAMES = ["__Host-arkray_csrftoken", "arkray_csrftoken"] as const;
export const CSRF_ENDPOINT = "/api/v1/auth/csrf";
const DEFAULT_TIMEOUT_MS = 15_000;
const UPLOAD_TIMEOUT_MS = 120_000;
const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly details: unknown = null,
    readonly requestId: string | null = null,
    readonly retryAfterSeconds: number | null = null,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export interface ApiRequestOptions {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  body?: unknown;
  signal?: AbortSignal;
  timeoutMs?: number;
  /** Extra request headers (e.g. Idempotency-Key). Never credentials: the session is a cookie. */
  headers?: Record<string, string>;
}

export function readCsrfToken(cookies: string = document.cookie): string | null {
  for (const name of CSRF_COOKIE_NAMES) {
    const token = readCookie(name, cookies);
    if (token) return token;
  }
  return null;
}

export function readCookie(name: string, cookies: string = document.cookie): string | null {
  for (const part of cookies.split(";")) {
    const [key, ...rest] = part.trim().split("=");
    if (key !== name) continue;
    const value = rest.join("=");
    try {
      return decodeURIComponent(value);
    } catch {
      return value; // malformed encoding (e.g. planted by another site): let the server decide
    }
  }
  return null;
}

async function readJson(response: Response): Promise<unknown> {
  const type = response.headers.get("Content-Type") ?? "";
  if (!type.includes("application/json")) return null;
  try {
    return await response.json();
  } catch {
    return null;
  }
}

function retryAfter(response: Response): number | null {
  const value = Number(response.headers.get("Retry-After"));
  return Number.isFinite(value) && value > 0 ? value : null;
}

function toApiError(response: Response, payload: unknown): ApiError {
  const envelope = (payload as { error?: Record<string, unknown> } | null)?.error;
  if (envelope && typeof envelope.code === "string" && typeof envelope.message === "string") {
    return new ApiError(
      response.status,
      envelope.code,
      envelope.message,
      envelope.details ?? null,
      typeof envelope.request_id === "string" ? envelope.request_id : null,
      retryAfter(response),
    );
  }
  return new ApiError(
    response.status,
    "http_error",
    `Something went wrong (HTTP ${response.status}). Please try again.`,
  );
}

/** A request body and its type; encoded per attempt (a retry sends it again). */
interface Payload {
  encode: () => BodyInit;
  contentType: string;
}

async function send(
  path: string,
  method: string,
  payload: Payload | undefined,
  signal: AbortSignal,
  extraHeaders: Record<string, string> = {},
): Promise<Response> {
  const headers: Record<string, string> = { ...extraHeaders, Accept: "application/json" };
  if (payload !== undefined) headers["Content-Type"] = payload.contentType;
  if (!SAFE_METHODS.has(method)) {
    const token = readCsrfToken();
    if (token) headers["X-CSRFToken"] = token;
  }
  return fetch(path, {
    method,
    headers,
    body: payload === undefined ? undefined : payload.encode(),
    credentials: "same-origin",
    signal,
  });
}

async function refreshCsrfCookie(signal: AbortSignal): Promise<void> {
  await fetch(CSRF_ENDPOINT, { method: "GET", credentials: "same-origin", signal });
}

async function request<T>(
  path: string,
  method: string,
  body: Payload | undefined,
  { signal, timeoutMs, headers }: { signal?: AbortSignal; timeoutMs: number; headers?: Record<string, string> },
): Promise<T> {
  const timeout = AbortSignal.timeout(timeoutMs);
  const combined = signal ? AbortSignal.any([signal, timeout]) : timeout;
  const unsafe = !SAFE_METHODS.has(method);

  let response: Response;
  let payload: unknown;
  try {
    if (unsafe && !readCsrfToken()) await refreshCsrfCookie(combined);
    response = await send(path, method, body, combined, headers);
    payload = await readJson(response);
    if (unsafe && response.status === 403 && toApiError(response, payload).code === "csrf_failed") {
      await refreshCsrfCookie(combined);
      response = await send(path, method, body, combined, headers);
      payload = await readJson(response);
    }
  } catch (error) {
    if (signal?.aborted) throw error; // cancelled by the caller: not an error to report
    if (timeout.aborted) {
      // A write may have been applied even though the answer never arrived.
      const message = unsafe
        ? "The server took too long to respond. Refresh to check whether the change was saved before trying again."
        : "The server took too long to respond. Please try again.";
      throw new ApiError(0, "timeout", message);
    }
    if (error instanceof TypeError) {
      throw new ApiError(0, "network_error", "Could not reach the server. Check your connection.");
    }
    throw error; // a bug, not the network: don't disguise it
  }

  if (response.status === 204) return undefined as T;
  if (!response.ok) throw toApiError(response, payload);
  return payload as T;
}

/**
 * Creates the server refuses without an Idempotency-Key (docs/api-conventions.md#idempotency).
 * Checked here as well, before anything is sent: a create without a key, or with one that
 * isn't a UUID, is a bug in the calling code, never a request to make.
 */
const KEY_REQUIRED: readonly RegExp[] = [/^\/api\/v1\/workspaces\/[^/?#]+\/opportunities\/?(?:\?.*)?$/];

function requireIdempotencyKey(path: string, method: string, headers: Record<string, string> | undefined): void {
  if (method !== "POST" || !KEY_REQUIRED.some((pattern) => pattern.test(path))) return;
  if (!isIdempotencyKey(headers?.["Idempotency-Key"])) {
    throw new Error(`POST ${path} needs an Idempotency-Key (a new UUID per new record).`);
  }
}

export async function apiFetch<T>(path: string, options: ApiRequestOptions = {}): Promise<T> {
  if (!path.startsWith("/api/")) {
    throw new Error(`apiFetch only calls same-origin API paths, got: ${path}`);
  }
  const { method = "GET", body, signal, timeoutMs = DEFAULT_TIMEOUT_MS, headers } = options;
  requireIdempotencyKey(path, method, headers);
  const payload = body === undefined ? undefined : { encode: () => JSON.stringify(body), contentType: "application/json" };
  return request<T>(path, method, payload, { signal, timeoutMs, headers });
}

export interface ApiUploadOptions {
  signal?: AbortSignal;
  /** Defaults to 2 minutes: a 10 MB file on a slow connection. */
  timeoutMs?: number;
  headers?: Record<string, string>;
}

/**
 * POSTs one file as the raw request body (application/octet-stream), its name in the
 * X-Filename header (percent-encoded UTF-8). CSRF, retries, timeouts and errors as apiFetch.
 */
export async function apiUpload<T>(path: string, file: Blob, filename: string, options: ApiUploadOptions = {}): Promise<T> {
  if (!path.startsWith("/api/")) {
    throw new Error(`apiUpload only calls same-origin API paths, got: ${path}`);
  }
  const { signal, timeoutMs = UPLOAD_TIMEOUT_MS, headers } = options;
  return request<T>(
    path,
    "POST",
    { encode: () => file, contentType: "application/octet-stream" },
    { signal, timeoutMs, headers: { ...headers, "X-Filename": encodeURIComponent(filename) } },
  );
}
