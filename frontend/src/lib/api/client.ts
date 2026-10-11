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
 * - A request refused with 403 `mfa_required` (a recent second factor is needed, ADR-0033)
 *   asks the registered step-up handler for one and is then sent again, once (see below).
 */
import { isIdempotencyKey } from "@/lib/random";

/**
 * Over HTTPS the backend names its CSRF cookie `__Host-arkray_csrftoken` (Secure, host-only,
 * Path=/: a sibling subdomain can't set or shadow it); a plain-HTTP local stack can't use the
 * prefix and keeps `arkray_csrftoken`. The prefixed cookie wins when both exist.
 */
export const CSRF_COOKIE_NAMES = ["__Host-arkray_csrftoken", "arkray_csrftoken"] as const;
/**
 * The session cookie, named the same way. HTTP-only: never read here, only checked for by the
 * server render (app/layout), to tell a session that ended from one that never began.
 */
export const SESSION_COOKIE_NAMES = ["__Host-arkray_session", "arkray_session"] as const;
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
  // The edge proxy names every response, its own errors included (a 502 page has no
  // envelope): the header is the fallback reference a user can quote.
  const headerId = response.headers.get("X-Request-ID");
  if (envelope && typeof envelope.code === "string" && typeof envelope.message === "string") {
    return new ApiError(
      response.status,
      envelope.code,
      envelope.message,
      envelope.details ?? null,
      typeof envelope.request_id === "string" ? envelope.request_id : headerId,
      retryAfter(response),
    );
  }
  return new ApiError(
    response.status,
    "http_error",
    `Something went wrong (HTTP ${response.status}). Please try again.`,
    null,
    headerId,
    retryAfter(response),
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

// --- Step-up verification (ADR-0033) ---------------------------------------------------------
/**
 * Asks the person for a second factor and verifies it with the server (the web app's
 * StepUpProvider does, with a dialog). Resolves true once verified, false when they cancel.
 * The code never passes through here.
 */
export type StepUpHandler = (error: ApiError) => Promise<boolean>;

let stepUpHandler: StepUpHandler | null = null;
/** The prompt on screen: every request refused meanwhile waits for this same one. */
let stepUpPrompt: Promise<boolean> | null = null;
/** Counts successful verifications: a request sent before the latest one is retried
 * without asking again (its refusal predates the verification). */
let stepUpGeneration = 0;

/** Installs the handler; returns the function that removes it again. */
export function registerStepUpHandler(handler: StepUpHandler): () => void {
  stepUpHandler = handler;
  return () => {
    if (stepUpHandler === handler) stepUpHandler = null;
  };
}

/** The sign-in and two-step endpoints themselves never ask for a step-up (no loops). */
const STEP_UP_EXEMPT_PREFIX = "/api/v1/auth/";

function needsStepUp(error: unknown, path: string): error is ApiError {
  return error instanceof ApiError && error.status === 403 && error.code === "mfa_required" && !path.startsWith(STEP_UP_EXEMPT_PREFIX);
}

/**
 * Runs a request; when the server wants a recent second factor, asks for it (one prompt
 * shared by every request refused at the same time) and repeats the request exactly once.
 * The repeat's answer is final, even another `mfa_required`: never a loop. Cancelling, or
 * the caller giving up meanwhile, rejects with the original refusal. The repeat is safe for
 * the same reason as the CSRF retry: the server refused before doing anything.
 */
async function withStepUp<T>(path: string, run: () => Promise<T>, signal: AbortSignal | undefined): Promise<T> {
  const generation = stepUpGeneration;
  try {
    return await run();
  } catch (error) {
    const handler = stepUpHandler;
    if (!handler || !needsStepUp(error, path)) throw error;
    if (generation === stepUpGeneration) {
      stepUpPrompt ??= handler(error)
        .catch(() => false)
        .then((verified) => {
          if (verified) stepUpGeneration += 1;
          return verified;
        })
        .finally(() => {
          stepUpPrompt = null;
        });
      const verified = await stepUpPrompt;
      if (!verified) throw error;
    }
    if (signal?.aborted) throw error;
    return run();
  }
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
  return withStepUp(path, () => request<T>(path, method, payload, { signal, timeoutMs, headers }), signal);
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
  return withStepUp(
    path,
    () =>
      request<T>(
        path,
        "POST",
        { encode: () => file, contentType: "application/octet-stream" },
        { signal, timeoutMs, headers: { ...headers, "X-Filename": encodeURIComponent(filename) } },
      ),
    signal,
  );
}
