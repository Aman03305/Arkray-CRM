import { describe, expect, it } from "vitest";

import { ApiError } from "./client";
import { describeError, fieldErrors, isApiError, tryAgainIn } from "./errors";

describe("describeError", () => {
  it("never shows raw exceptions", () => {
    expect(describeError(new TypeError("x is undefined at foo.js:12")).message).toBe(
      "Something went wrong. Please try again.",
    );
  });

  it("says the service is down, and for how long when the server says (audit)", () => {
    expect(describeError(new ApiError(503, "service_unavailable", "x", null, "r", 30)).message).toBe(
      "Arkray is temporarily unavailable. Please try again in 30 seconds.",
    );
    expect(describeError(new ApiError(503, "service_unavailable", "x")).message).toBe(
      "Arkray is temporarily unavailable. Please try again in a moment.",
    );
  });

  it("uses a generic message (with the reference) for server errors", () => {
    const shown = describeError(new ApiError(500, "server_error", "Traceback ...", null, "req-9"));
    expect(shown).toEqual({
      message: "Something went wrong on our side. Please try again in a moment.",
      requestId: "req-9",
    });
  });

  it("explains authorization errors plainly", () => {
    expect(describeError(new ApiError(403, "permission_denied", "x")).message).toBe(
      "You don't have permission to do that.",
    );
  });

  it("explains support-session and password-change refusals plainly", () => {
    expect(describeError(new ApiError(403, "support_session_active", "Not allowed in a support session.")).message).toBe(
      "Not available during a support session. Exit it first.",
    );
    expect(describeError(new ApiError(403, "password_change_required", "Change your password.")).message).toBe(
      "Choose a new password to continue.",
    );
  });

  it("passes safe envelope messages through", () => {
    const error = new ApiError(429, "rate_limited", "Too many sign-in attempts. Try again in 1 minute.");
    expect(describeError(error).message).toBe("Too many sign-in attempts. Try again in 1 minute.");
  });

  it("says when to try again after a 429 that carries Retry-After", () => {
    const limited = (message: string, wait: number | null) => describeError(new ApiError(429, "rate_limited", message, null, "r", wait)).message;
    expect(limited("Too many attempts. Please try again later.", 30)).toBe("Too many attempts. Try again in 30 seconds.");
    expect(limited("Too many new opportunities at once.", 1)).toBe("Too many new opportunities at once. Try again in 1 second.");
    expect(limited("", 45)).toBe("Too many requests. Try again in 45 seconds.");
    // Already said, or no Retry-After: as sent.
    expect(limited("Too many sign-in attempts. Try again in 1 minute.", 60)).toBe("Too many sign-in attempts. Try again in 1 minute.");
    expect(limited("Too many attempts. Please try again later.", null)).toBe("Too many attempts. Please try again later.");
    expect(describeError(new ApiError(429, "rate_limited", "Slow down.", null, "req-9", 5)).requestId).toBe("req-9");
  });

  it("never shows the request throttle's own text (E2E-2)", () => {
    const throttled = (wait: number | null) =>
      describeError(new ApiError(429, "rate_limited", "Request was throttled. Expected available in 37 seconds.", null, "r", wait)).message;
    expect(throttled(37)).toBe("Too many requests. Try again in about a minute.");
    expect(throttled(null)).toBe("Too many requests. Try again later.");
  });

  it("replaces 'Try again later.' with the wait rather than adding a second sentence", () => {
    const error = new ApiError(429, "rate_limited", "Too many password reset requests. Try again later.", null, "r", 30);
    expect(describeError(error).message).toBe("Too many password reset requests. Try again in 30 seconds.");
  });

  it("gives a single-purpose form one consistent message whichever limit was hit (E2E-2)", () => {
    const signIn = (message: string, wait: number | null) =>
      describeError(new ApiError(429, "rate_limited", message, null, "r", wait), { tooMany: "sign-in attempts" }).message;
    expect(signIn("Request was throttled. Expected available in 37 seconds.", 37)).toBe(
      "Too many sign-in attempts. Try again in about a minute.",
    );
    expect(signIn("Too many sign-in attempts. Try again in 15 minutes.", 900)).toBe(
      "Too many sign-in attempts. Try again in about 15 minutes.",
    );
    expect(signIn("Request was throttled.", null)).toBe("Too many sign-in attempts. Try again later.");
    // No Retry-After: the server's own message is all there is to go on.
    expect(signIn("Too many sign-in attempts. Try again in 1 minute.", null)).toBe("Too many sign-in attempts. Try again in 1 minute.");
    // Other errors are described as usual.
    expect(describeError(new ApiError(400, "invalid_credentials", "Invalid email or password."), { tooMany: "sign-in attempts" }).message).toBe(
      "Invalid email or password.",
    );
  });
});

describe("tryAgainIn", () => {
  it("rounds Retry-After to words people use", () => {
    expect(tryAgainIn(null)).toBe("later");
    expect(tryAgainIn(0)).toBe("later");
    expect(tryAgainIn(3)).toBe("in a few seconds");
    expect(tryAgainIn(10)).toBe("in a few seconds");
    expect(tryAgainIn(11)).toBe("in about a minute");
    expect(tryAgainIn(37)).toBe("in about a minute");
    expect(tryAgainIn(89)).toBe("in about a minute");
    expect(tryAgainIn(90)).toBe("in about 2 minutes");
    expect(tryAgainIn(900)).toBe("in about 15 minutes");
    expect(tryAgainIn(3540)).toBe("in about 59 minutes");
    expect(tryAgainIn(3600)).toBe("in about an hour");
    expect(tryAgainIn(4 * 3600)).toBe("in about 4 hours");
  });
});

describe("fieldErrors", () => {
  it("maps validation details to fields", () => {
    const error = new ApiError(400, "validation_error", "Invalid.", {
      email: ["Enter a valid email address."],
      role: "Bad.",
    });
    expect(fieldErrors(error)).toEqual({ email: ["Enter a valid email address."], role: ["Bad."] });
  });

  it("is empty for anything else", () => {
    expect(fieldErrors(new Error("x"))).toEqual({});
    expect(fieldErrors(new ApiError(400, "invalid_credentials", "No.", null))).toEqual({});
  });
});

it("isApiError matches status and optional code", () => {
  const error = new ApiError(400, "invalid_token", "x");
  expect(isApiError(error, 400)).toBe(true);
  expect(isApiError(error, 400, "invalid_token")).toBe(true);
  expect(isApiError(error, 400, "other")).toBe(false);
  expect(isApiError(new Error("x"), 400)).toBe(false);
});
