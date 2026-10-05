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

export function describeError(error: unknown): DisplayError {
  if (!(error instanceof ApiError)) return { message: GENERIC, requestId: null };
  if (error.status === 503) {
    const wait = error.retryAfterSeconds;
    return {
      message: `Arkray is temporarily unavailable. Please try again ${wait ? `in ${wait} seconds` : "in a moment"}.`,
      requestId: error.requestId,
    };
  }
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
