/**
 * Money and percentages, handled as decimal STRINGS end to end (docs/pipeline.md#money).
 *
 * The API sends amounts as exact decimal strings ("1250000.50") and the backend computes
 * every figure (weighted values, pipeline totals) in PostgreSQL NUMERIC. The browser only
 * formats and validates text: nothing here converts an amount to a JavaScript Number, so an
 * amount can never be rounded through binary floating point (0.1 + 0.2), lose paise above
 * 2^53, or display differently from what the server stored.
 */

/** Organisation currency (settings.CRM_CURRENCY; the API echoes it in aggregate payloads). */
export const CURRENCY = "INR";
export const MAX_WHOLE_DIGITS = 12; // NUMERIC(14, 2): up to 999,999,999,999.99

const DECIMAL = /^(-?)(\d+)(?:\.(\d+))?$/;

/** Indian digit grouping (lakh, crore): 1250000 -> "12,50,000", 1234 -> "1,234". */
export function groupIndian(digits: string): string {
  const whole = digits.replace(/^0+(?=\d)/, "");
  if (whole.length <= 3) return whole;
  const last3 = whole.slice(-3);
  const rest = whole.slice(0, -3);
  const pairs = rest.replace(/\B(?=(\d{2})+(?!\d))/g, ",");
  return `${pairs},${last3}`;
}

/**
 * "1250000.00" -> "₹12,50,000"; "1250000.5" -> "₹12,50,000.50". Paise are shown only when
 * present, always as two digits. Anything that isn't a decimal string is shown as "—",
 * never guessed at.
 */
export function formatInr(value: string | null | undefined, { paise = "auto" }: { paise?: "auto" | "always" } = {}): string {
  if (value === null || value === undefined) return "—";
  const match = DECIMAL.exec(value.trim());
  if (!match) return "—";
  const [, sign, whole, fraction = ""] = match;
  const cents = (fraction + "00").slice(0, 2); // the API always sends two places
  const showPaise = paise === "always" || /[1-9]/.test(cents);
  return `${sign}₹${groupIndian(whole!)}${showPaise ? `.${cents}` : ""}`;
}

/** A compact label for tight spaces: "₹12.5 L", "₹3.2 Cr", "₹75,000". Display only. */
export function formatInrCompact(value: string | null | undefined): string {
  if (value === null || value === undefined) return "—";
  const match = DECIMAL.exec(value.trim());
  if (!match) return "—";
  const whole = match[2]!.replace(/^0+(?=\d)/, "");
  const unit = whole.length > 7 ? { size: 7, label: "Cr" } : whole.length > 5 ? { size: 5, label: "L" } : null;
  if (!unit) return formatInr(value);
  // Truncate (never round up) to one decimal of the unit, from the digits themselves.
  const head = whole.slice(0, whole.length - unit.size);
  const tenth = whole.charAt(whole.length - unit.size);
  return `${match[1]}₹${groupIndian(head)}${tenth === "0" ? "" : `.${tenth}`} ${unit.label}`;
}

/** "75.00" -> "75%", "62.50" -> "62.5%", "33.33" -> "33.33%". */
export function formatPercent(value: string | null | undefined): string {
  if (value === null || value === undefined) return "—";
  const match = DECIMAL.exec(value.trim());
  if (!match) return "—";
  const fraction = (match[3] ?? "").replace(/0+$/, "");
  return `${match[2]!.replace(/^0+(?=\d)/, "")}${fraction ? `.${fraction}` : ""}%`;
}

export type Parsed = { ok: true; value: string } | { ok: false; error: string };

/**
 * What a person typed in an amount field -> the canonical string the API accepts.
 * Accepted: digits with Indian or international grouping commas, an optional ₹ or "Rs",
 * spaces, and up to two decimal places ("12,50,000", "1250000.5", "₹ 1,250,000.50").
 * Refused, never "fixed": signs, exponents, other digits (full-width, Devanagari), more
 * than two decimal places, more than 12 whole digits.
 */
const INDIAN_GROUPS = /^\d{1,2}(?:,\d{2})*,\d{3}$/; // 12,50,000
const INTERNATIONAL_GROUPS = /^\d{1,3}(?:,\d{3})+$/; // 1,250,000

export function parseAmountInput(input: string): Parsed {
  const spaced = input.trim().replace(/^(?:₹|rs\.?|inr)\s*/i, "").replace(/\s/g, "");
  if (!spaced) return { ok: false, error: "Enter an amount." };
  const [wholeTyped = ""] = spaced.split(".");
  // A misplaced comma usually means a missing or extra digit ("12,50,00"): refuse it
  // rather than guess an amount ten times smaller or larger (review).
  if (wholeTyped.includes(",") && !INDIAN_GROUPS.test(wholeTyped) && !INTERNATIONAL_GROUPS.test(wholeTyped)) {
    return { ok: false, error: "Check the commas: write the amount like 12,50,000 or 1,250,000 (or without commas)." };
  }
  const text = spaced.replace(/,/g, "");
  const match = /^([0-9]+)(?:\.([0-9]{1,2}))?$/.exec(text);
  if (!match) {
    return { ok: false, error: "Enter an amount in rupees, e.g. 12,50,000 or 1250000.50 (at most 2 decimal places)." };
  }
  const whole = match[1]!.replace(/^0+(?=\d)/, "");
  if (whole.length > MAX_WHOLE_DIGITS) return { ok: false, error: "The amount must be at most ₹9,99,99,99,99,999.99." };
  return { ok: true, value: match[2] ? `${whole}.${match[2]}` : whole };
}

/** A percentage 0-100 with up to two decimals ("62.5", "100", "0.25"), compared as text. */
export function parsePercentInput(input: string): Parsed {
  const text = input.trim().replace(/%$/, "").trim();
  const match = /^([0-9]{1,3})(?:\.([0-9]{1,2}))?$/.exec(text);
  const error = "Enter a percentage from 0 to 100 (at most 2 decimal places).";
  if (!match) return { ok: false, error };
  const whole = match[1]!.replace(/^0+(?=\d)/, "");
  const fraction = match[2] ?? "";
  if (whole.length === 3 && (whole !== "100" || /[1-9]/.test(fraction))) return { ok: false, error };
  return { ok: true, value: fraction ? `${whole}.${fraction}` : whole };
}

/** A stored amount as the text an amount field starts with ("1250000.00" -> "1250000"). */
export function amountInputValue(value: string | null | undefined): string {
  if (!value) return "";
  const match = DECIMAL.exec(value.trim());
  if (!match) return "";
  const fraction = (match[3] ?? "").replace(/0+$/, "");
  return `${match[2]}${fraction ? `.${fraction}` : ""}`;
}

/** Same number? Compared as canonical decimal text ("50", "50.0" and "50.00" are equal). */
export function sameDecimal(a: string | null | undefined, b: string | null | undefined): boolean {
  const canonical = (v: string | null | undefined) => {
    const match = DECIMAL.exec((v ?? "").trim());
    if (!match) return null;
    const whole = match[2]!.replace(/^0+(?=\d)/, "");
    const fraction = (match[3] ?? "").replace(/0+$/, "");
    return `${match[1]}${whole}${fraction ? `.${fraction}` : ""}`;
  };
  const left = canonical(a);
  return left !== null && left === canonical(b);
}
