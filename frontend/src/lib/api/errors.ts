/**
 * Turning API failures into what the UI shows. Messages come from the backend's error
 * envelope (always safe to display) or from the fixed texts below; raw exceptions and
 * response bodies are never shown.
 */
import { ApiError } from "./client";

export interface DisplayError {
  message: string;
  requestId: string | null;
}

const GENERIC = "Something went wrong. Please try again.";

export interface DescribeOptions {
  /**
   * What a 429 refused, for a form with one purpose (sign-in, password reset): the message is
   * then "Too many {tooMany}. Try again in about a minute." whichever limit the server hit,
   * the general request throttle or the account's own (E2E-2).
   */
  tooMany?: string;
}

export function describeError(error: unknown, { tooMany }: DescribeOptions = {}): DisplayError {
  if (!(error instanceof ApiError)) return { message: GENERIC, requestId: null };
  if (error.status === 503) {
    const wait = error.retryAfterSeconds;
    return {
      message: `Arkray is temporarily unavailable. Please try again ${wait ? `in ${wait} seconds` : "in a moment"}.`,
      requestId: error.requestId,
    };
  }
  if (error.status === 429) return { message: rateLimited(error, tooMany), requestId: error.requestId };
  if (error.status >= 500) {
    return {
      message: "Something went wrong on our side. Please try again in a moment.",
      requestId: error.requestId,
    };
  }
  if (error.status === 403 && error.code === "permission_denied") {
    return { message: "You don't have permission to do that.", requestId: error.requestId };
  }
  if (error.status === 403 && error.code === "support_session_active") {
    return { message: "Not available during a support session. Exit it first.", requestId: error.requestId };
  }
  if (error.status === 403 && error.code === "password_change_required") {
    return { message: "Choose a new password to continue.", requestId: error.requestId };
  }
  return { message: error.message || GENERIC, requestId: error.requestId };
}

/** The request throttle's own words ("Request was throttled. Expected available in 37
 * seconds."): never shown as they are (E2E-2). */
const THROTTLE_TEXT = /^Request was throttled\b/i;

/** A 429 says when to try again when the server sent Retry-After, unless its message already
 * does ("Try again in 1 minute."). */
function rateLimited(error: ApiError, tooMany: string | undefined): string {
  const wait = error.retryAfterSeconds;
  const raw = THROTTLE_TEXT.test(error.message);
  // Without Retry-After, the server's own words (never the throttle's) are all there is to go on.
  if (tooMany && (wait !== null || raw || !error.message)) return `Too many ${tooMany}. Try again ${tryAgainIn(wait)}.`;
  if (raw) return `Too many requests. Try again ${tryAgainIn(wait)}.`;
  const message = error.message || "Too many requests.";
  if (!wait || /try again in|available in/i.test(message)) return message;
  const when = `Try again in ${wait === 1 ? "1 second" : `${wait} seconds`}.`;
  // "Please try again later." / "Try again later." becomes the actual wait, never both.
  const later = /(?:Please )?try again later\.$/i;
  return later.test(message) ? message.replace(later, when) : `${message} ${when}`;
}

/**
 * A wait in words people use, from Retry-After seconds: "in a few seconds", "in about a
 * minute", "in about 15 minutes", "in about an hour"; "later" when the server didn't say.
 */
export function tryAgainIn(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds) || seconds <= 0) return "later";
  if (seconds <= 10) return "in a few seconds";
  if (seconds < 90) return "in about a minute";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `in about ${minutes} minutes`;
  const hours = Math.round(seconds / 3600);
  return hours === 1 ? "in about an hour" : `in about ${hours} hours`;
}

/** Field -> messages from a validation (400) or uniqueness (409) error; {} otherwise. */
export function fieldErrors(error: unknown): Record<string, string[]> {
  if (!(error instanceof ApiError) || typeof error.details !== "object" || error.details === null) {
    return {};
  }
  const result: Record<string, string[]> = {};
  for (const [field, messages] of Object.entries(error.details as Record<string, unknown>)) {
    if (Array.isArray(messages)) {
      result[field] = messages.filter((m): m is string => typeof m === "string");
    } else if (typeof messages === "string") {
      result[field] = [messages];
    }
  }
  return result;
}

export function isApiError(error: unknown, status: number, code?: string): error is ApiError {
  return error instanceof ApiError && error.status === status && (code === undefined || error.code === code);
}
