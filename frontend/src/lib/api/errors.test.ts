import { describe, expect, it } from "vitest";

import { ApiError } from "./client";
import { describeError, fieldErrors, isApiError } from "./errors";

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

  it("passes safe envelope messages through", () => {
    const error = new ApiError(429, "rate_limited", "Too many sign-in attempts. Try again in 1 minute.");
    expect(describeError(error).message).toBe("Too many sign-in attempts. Try again in 1 minute.");
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
