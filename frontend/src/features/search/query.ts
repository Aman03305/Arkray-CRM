/**
 * The search box's rules, mirroring the API's (backend core/text.py and core/ranking.py,
 * docs/search.md#input): 2-100 characters; words with 3 letters or digits in a row (a
 * letter's own marks count with it) are searched (a trigram index can look them up), so
 * there must be one; no
 * invisible or control characters (zero-width joiners excepted: Indic scripts need them).
 * Checking here only saves pointless requests; the API decides, and its 400 message is
 * shown if it disagrees.
 */
export const SEARCH_MAX_LENGTH = 100;

// As core/ranking.py's `indexable`: 3 letters or digits in a row, a letter's marks counting
// with it unless they are generic combining marks, enclosing marks or variation selectors
// ("शर्मा", not "---" or "on" plus a combining accent); joiners neither count nor break it.
const SEARCHABLE_WORD =
  /[\p{L}\p{Nd}\p{Nl}](?:[\u200C\u200D]*(?:[\p{L}\p{Nd}\p{Nl}]|(?![\u0300-\u036F\u1AB0-\u1AFF\u1DC0-\u1DFF\u20D0-\u20FF\uFE00-\uFE0F\uFE20-\uFE2F\u{E0100}-\u{E01EF}])[\p{Mn}\p{Mc}])){2}/u;

// As the API refuses them (core/text.py): control characters other than tab, newline and
// carriage return; surrogates; private use; line/paragraph separators; format characters
// other than ZWNJ (U+200C) and ZWJ (U+200D); noncharacters.
const REFUSED =
  /(?![\t\n\r])\p{Cc}|[\p{Cs}\p{Co}\p{Zl}\p{Zp}]|(?![\u200C\u200D])\p{Cf}|[\uFDD0-\uFDEF\uFFFE\uFFFF]|[\u{1FFFE}\u{1FFFF}\u{2FFFE}\u{2FFFF}\u{3FFFE}\u{3FFFF}\u{4FFFE}\u{4FFFF}\u{5FFFE}\u{5FFFF}\u{6FFFE}\u{6FFFF}\u{7FFFE}\u{7FFFF}\u{8FFFE}\u{8FFFF}\u{9FFFE}\u{9FFFF}\u{AFFFE}\u{AFFFF}\u{BFFFE}\u{BFFFF}\u{CFFFE}\u{CFFFF}\u{DFFFE}\u{DFFFF}\u{EFFFE}\u{EFFFF}\u{FFFFE}\u{FFFFF}\u{10FFFE}\u{10FFFF}]/u;

/** What is sent: NFC, every run of whitespace (any script's) collapsed to one space,
 * trimmed, as the API cleans it. */
export function normalizeQuery(raw: string): string {
  return raw.normalize("NFC").replace(/\s+/gu, " ").trim();
}

export type QueryState =
  | { kind: "idle" } // nothing long enough to search yet
  | { kind: "invalid"; message: string }
  | { kind: "ready"; query: string };

export function queryState(raw: string): QueryState {
  if (REFUSED.test(raw)) {
    return { kind: "invalid", message: "Remove the invisible or control characters from your search." };
  }
  const query = normalizeQuery(raw);
  // Characters as the API counts them (code points), not UTF-16 units.
  if (Array.from(query).length > SEARCH_MAX_LENGTH) {
    return { kind: "invalid", message: `Use at most ${SEARCH_MAX_LENGTH} characters.` };
  }
  if (!query.split(" ").some((word) => SEARCHABLE_WORD.test(word))) {
    return { kind: "idle" };
  }
  return { kind: "ready", query };
}
