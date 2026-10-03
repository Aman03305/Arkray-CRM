/**
 * Dates and times are shown in the organisation's business time zone (ADR-0012), not the
 * browser's, so every user sees the same "today". The API speaks UTC ISO 8601.
 */
export const BUSINESS_TIME_ZONE = "Asia/Kolkata";
const LOCALE = "en-IN";

const dateFormat = new Intl.DateTimeFormat(LOCALE, {
  timeZone: BUSINESS_TIME_ZONE,
  day: "numeric",
  month: "short",
  year: "numeric",
});

const dateTimeFormat = new Intl.DateTimeFormat(LOCALE, {
  timeZone: BUSINESS_TIME_ZONE,
  day: "numeric",
  month: "short",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  timeZoneName: "short",
});

function parse(iso: string | null | undefined): Date | null {
  if (!iso) return null;
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? null : date;
}

export function formatDate(iso: string | null | undefined, fallback = "—"): string {
  const date = parse(iso);
  return date ? dateFormat.format(date) : fallback;
}

export function formatDateTime(iso: string | null | undefined, fallback = "—"): string {
  const date = parse(iso);
  return date ? dateTimeFormat.format(date) : fallback;
}

const relative = new Intl.RelativeTimeFormat(LOCALE, { numeric: "auto" });
const UNITS: [Intl.RelativeTimeFormatUnit, number][] = [
  ["year", 365 * 24 * 3600],
  ["month", 30 * 24 * 3600],
  ["week", 7 * 24 * 3600],
  ["day", 24 * 3600],
  ["hour", 3600],
  ["minute", 60],
];

/** "3 hours ago", "in 2 days"; "just now" under a minute. */
export function formatRelative(iso: string | null | undefined, now: Date = new Date(), fallback = "—"): string {
  const date = parse(iso);
  if (!date) return fallback;
  const seconds = (date.getTime() - now.getTime()) / 1000;
  for (const [unit, size] of UNITS) {
    if (Math.abs(seconds) >= size) return relative.format(Math.round(seconds / size), unit);
  }
  return "just now";
}

// --- <input type="datetime-local"> in the business time zone ------------------------------
// The input has no time zone: its "2026-09-30T14:30" is read as a wall-clock time in the
// business zone (the same zone every date on screen uses), and sent to the API as UTC.

const wallClockParts = new Intl.DateTimeFormat("en-US", {
  timeZone: BUSINESS_TIME_ZONE,
  hourCycle: "h23",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
});

function businessWallClock(date: Date): { y: number; mo: number; d: number; h: number; mi: number; s: number } {
  const get = (type: Intl.DateTimeFormatPartTypes) =>
    Number(wallClockParts.formatToParts(date).find((p) => p.type === type)?.value ?? 0);
  return { y: get("year"), mo: get("month"), d: get("day"), h: get("hour"), mi: get("minute"), s: get("second") };
}

/** Minutes the business zone is ahead of UTC at `date` (e.g. 330 for Asia/Kolkata). */
function businessOffsetMinutes(date: Date): number {
  const w = businessWallClock(date);
  const asUtc = Date.UTC(w.y, w.mo - 1, w.d, w.h, w.mi, w.s);
  return Math.round((asUtc - Math.floor(date.getTime() / 1000) * 1000) / 60_000);
}

const pad = (n: number) => String(n).padStart(2, "0");

/** ISO instant -> "YYYY-MM-DDTHH:mm" in the business zone ("" when empty or invalid). */
export function toBusinessDateTimeInput(iso: string | null | undefined): string {
  const date = parse(iso);
  if (!date) return "";
  const w = businessWallClock(date);
  return `${w.y}-${pad(w.mo)}-${pad(w.d)}T${pad(w.h)}:${pad(w.mi)}`;
}

/** "YYYY-MM-DDTHH:mm[:ss]" (business zone) -> ISO instant in UTC; null when empty or invalid. */
export function fromBusinessDateTimeInput(value: string): string | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?$/.exec(value);
  if (!match) return null;
  const [y, mo, d, h, mi] = match.slice(1, 6).map(Number) as [number, number, number, number, number];
  const s = Number(match[6] ?? 0);
  if (mo < 1 || mo > 12 || d < 1 || d > 31 || h > 23 || mi > 59 || s > 59) return null;
  const guess = Date.UTC(y, mo - 1, d, h, mi, s);
  if (Number.isNaN(guess)) return null;
  // Two passes settle the offset even across a daylight-saving change.
  let utc = guess - businessOffsetMinutes(new Date(guess)) * 60_000;
  utc = guess - businessOffsetMinutes(new Date(utc)) * 60_000;
  return new Date(utc).toISOString();
}

// --- date-only values (expected close dates) ---------------------------------------------
// A business date ("2026-10-15") has no time and no time zone: it is formatted from its own
// digits (as a UTC calendar date), never parsed as an instant, so no browser zone can move
// it to the previous or next day.

const DATE_ONLY = /^(\d{4})-(\d{2})-(\d{2})$/;
const dateOnlyFormat = new Intl.DateTimeFormat(LOCALE, { timeZone: "UTC", day: "numeric", month: "short", year: "numeric" });

export function formatDateOnly(value: string | null | undefined, fallback = "—"): string {
  const match = value ? DATE_ONLY.exec(value) : null;
  if (!match) return fallback;
  const [y, m, d] = match.slice(1).map(Number) as [number, number, number];
  const date = new Date(Date.UTC(y, m - 1, d));
  if (date.getUTCFullYear() !== y || date.getUTCMonth() !== m - 1 || date.getUTCDate() !== d) return fallback;
  return dateOnlyFormat.format(date);
}

/** Today's business date ("YYYY-MM-DD") in the organisation's time zone. */
export function businessToday(now: Date = new Date()): string {
  const w = businessWallClock(now);
  return `${w.y}-${pad(w.mo)}-${pad(w.d)}`;
}
