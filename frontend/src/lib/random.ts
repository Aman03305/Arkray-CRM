/**
 * A random (v4) UUID from the platform's cryptographically secure generator: crypto.randomUUID,
 * or crypto.getRandomValues where randomUUID is missing (non-secure contexts). There is no
 * weaker fallback: without Web Crypto this throws rather than produce a guessable value, since
 * these values are idempotency keys and upload keys (a predictable or repeated key could replay
 * someone else's request or collide with another submission).
 */
export function randomUuid(): string {
  const webCrypto: Crypto | undefined = globalThis.crypto;
  if (typeof webCrypto?.randomUUID === "function") return webCrypto.randomUUID();
  if (typeof webCrypto?.getRandomValues !== "function") {
    throw new Error("A secure random number generator (Web Crypto) is required.");
  }
  const bytes = webCrypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6]! & 0x0f) | 0x40;
  bytes[8] = (bytes[8]! & 0x3f) | 0x80;
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

const CANONICAL_UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** An Idempotency-Key: only newIdempotencyKey() makes one, so a create that requires a key
 * can't be handed an arbitrary (or empty) string by accident. */
export type IdempotencyKey = string & { readonly __brand: "IdempotencyKey" };

export function newIdempotencyKey(): IdempotencyKey {
  return randomUuid() as IdempotencyKey;
}

export function isIdempotencyKey(value: unknown): value is IdempotencyKey {
  return typeof value === "string" && CANONICAL_UUID.test(value);
}
