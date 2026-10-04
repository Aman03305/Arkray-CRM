import { Fragment } from "react";

export type Range = readonly [start: number, end: number];

/**
 * Where the searched words occur in `text` (case-insensitive), as merged, sorted ranges.
 * Text whose upper-case form has another length (German ß becomes SS, for instance) isn't
 * highlighted at all: positions in the folded text wouldn't be positions in the original.
 */
export function matchRanges(text: string, terms: readonly string[]): Range[] {
  const folded = text.toUpperCase();
  if (folded.length !== text.length) return [];
  const ranges: [number, number][] = [];
  for (const term of terms) {
    const needle = term.toUpperCase();
    if (!needle || needle.length !== term.length) continue;
    for (let at = folded.indexOf(needle); at !== -1; at = folded.indexOf(needle, at + needle.length)) {
      ranges.push([at, at + needle.length]);
    }
  }
  ranges.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  const merged: [number, number][] = [];
  for (const [start, end] of ranges) {
    const last = merged[merged.length - 1];
    if (last && start <= last[1]) last[1] = Math.max(last[1], end);
    else merged.push([start, end]);
  }
  return merged;
}

/**
 * `text` with the searched words marked. Built from React text nodes and <mark> elements
 * only (never HTML strings), so names, titles and notes render as the characters they are,
 * whatever they contain.
 */
export function Highlight({ text, terms }: { text: string; terms: readonly string[] }) {
  const ranges = matchRanges(text, terms);
  if (ranges.length === 0) return <>{text}</>;
  const parts = [];
  let at = 0;
  for (const [start, end] of ranges) {
    if (start > at) parts.push(<Fragment key={`t${at}`}>{text.slice(at, start)}</Fragment>);
    parts.push(
      <mark key={`m${start}`} className="rounded-sm bg-amber-100 px-0 text-inherit">
        {text.slice(start, end)}
      </mark>,
    );
    at = end;
  }
  if (at < text.length) parts.push(<Fragment key={`t${at}`}>{text.slice(at)}</Fragment>);
  return <>{parts}</>;
}
