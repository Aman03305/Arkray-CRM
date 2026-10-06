import { afterEach, describe, expect, it, vi } from "vitest";

import { isIdempotencyKey, newIdempotencyKey, randomUuid } from "./random";

const V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("randomUuid", () => {
  it("is a v4 UUID from crypto.randomUUID, never Math.random", () => {
    const random = vi.spyOn(Math, "random");
    const fromCrypto = vi.spyOn(globalThis.crypto, "randomUUID");
    const seen = new Set(Array.from({ length: 200 }, () => randomUuid()));
    expect(seen.size).toBe(200);
    for (const value of seen) expect(value).toMatch(V4);
    expect(fromCrypto).toHaveBeenCalledTimes(200);
    expect(random).not.toHaveBeenCalled();
  });

  it("falls back to crypto.getRandomValues (non-secure contexts), still a v4 UUID", () => {
    const real = globalThis.crypto;
    const getRandomValues = vi.fn((array: Uint8Array) => real.getRandomValues(array));
    vi.stubGlobal("crypto", { getRandomValues });
    const random = vi.spyOn(Math, "random");
    const values = Array.from({ length: 50 }, () => randomUuid());
    expect(new Set(values).size).toBe(50);
    for (const value of values) expect(value).toMatch(V4);
    expect(getRandomValues).toHaveBeenCalledTimes(50);
    expect(random).not.toHaveBeenCalled();
  });

  it("throws rather than produce a guessable value without Web Crypto", () => {
    const random = vi.spyOn(Math, "random");
    vi.stubGlobal("crypto", undefined);
    expect(() => randomUuid()).toThrow(/secure random/);
    vi.stubGlobal("crypto", {});
    expect(() => randomUuid()).toThrow(/secure random/);
    expect(random).not.toHaveBeenCalled();
  });
});

describe("idempotency keys", () => {
  it("are new UUIDs each time, and only UUIDs pass as one", () => {
    const a = newIdempotencyKey();
    const b = newIdempotencyKey();
    expect(a).not.toBe(b);
    expect(isIdempotencyKey(a)).toBe(true);
    for (const value of ["", "1", "not-a-uuid", `${a}, ${b}`, ` ${a}`, undefined, null, 42]) {
      expect(isIdempotencyKey(value)).toBe(false);
    }
  });
});
