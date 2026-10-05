/**
 * A strong initial password for an administrator to hand over: 16 characters from an
 * alphabet without look-alikes (no 0/O, 1/l/I), in four groups so it can be read out or
 * typed without mistakes (about 92 bits). Made in the browser with the platform's
 * cryptographic generator; never stored, logged or sent anywhere but the create request.
 */
const ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789";
const GROUPS = 4;
const GROUP_LENGTH = 4;

/** Uniformly random characters (rejection sampling: no modulo bias). */
function randomCharacters(count: number): string {
  const limit = 256 - (256 % ALPHABET.length);
  let out = "";
  while (out.length < count) {
    for (const byte of crypto.getRandomValues(new Uint8Array(count * 2))) {
      if (byte < limit) out += ALPHABET[byte % ALPHABET.length];
      if (out.length === count) break;
    }
  }
  return out;
}

export function generatePassword(): string {
  const characters = randomCharacters(GROUPS * GROUP_LENGTH);
  const groups = Array.from({ length: GROUPS }, (_, i) => characters.slice(i * GROUP_LENGTH, (i + 1) * GROUP_LENGTH));
  return groups.join("-");
}
