/**
 * Accessibility checks for a rendered view, in the terms a screen-reader user meets them:
 * the heading outline (one h1, no skipped levels) and the names of the controls (every
 * button and link has one, and a control repeated down a list says which row it is for).
 *
 * Names are the ones Testing Library computes for role queries (the accessible-name
 * algorithm), so a check here agrees with `getByRole(..., { name })`. Target sizes can't be
 * measured in jsdom; those are checked through the classes that give the size.
 */
import { queryAllByRole } from "@testing-library/react";

export interface NamedControl {
  role: string;
  name: string;
  element: HTMLElement;
}

const CONTROL_ROLES = ["button", "link", "menuitem", "textbox", "combobox", "checkbox", "radio", "searchbox", "spinbutton"];

/** Every control below `container` that assistive technology can reach, with its name. */
export function namedControls(container: HTMLElement = document.body, roles: readonly string[] = CONTROL_ROLES): NamedControl[] {
  const found: NamedControl[] = [];
  for (const role of roles) {
    queryAllByRole(container, role, {
      // A name matcher sees every candidate's computed name: record it, match them all.
      name: (name, element) => {
        found.push({ role, name: name.replace(/\s+/g, " ").trim(), element: element as HTMLElement });
        return true;
      },
    });
  }
  return found;
}

function describe(control: NamedControl): string {
  const html = control.element.outerHTML.replace(/\s+/g, " ");
  return `${control.role} "${control.name}" ${html.length > 160 ? `${html.slice(0, 160)}…` : html}`;
}

/** Controls without an accessible name (an icon-only button with no label, say). */
export function unnamedControls(container: HTMLElement = document.body): string[] {
  return namedControls(container)
    .filter((control) => control.name === "")
    .map(describe);
}

/**
 * Buttons (and menu items) that share a name: on a list, each row's "Edit" must say which
 * row it edits. Links sharing a name are ambiguous only when they lead to different places.
 * `exclude`: a selector for a layout CSS hides at the width being checked (jsdom applies no
 * stylesheet, so a table and its phone-sized card list are both "visible" here).
 */
export function ambiguousControls(container: HTMLElement = document.body, { exclude }: { exclude?: string } = {}): string[] {
  const groups = new Map<string, NamedControl[]>();
  for (const control of namedControls(container, ["button", "link", "menuitem"])) {
    if (!control.name || (exclude && control.element.closest(exclude))) continue;
    const key = `${control.role}|${control.name.toLowerCase()}`;
    groups.set(key, [...(groups.get(key) ?? []), control]);
  }
  const problems: string[] = [];
  for (const group of groups.values()) {
    if (group.length < 2) continue;
    const first = group[0]!;
    if (first.role === "link") {
      const targets = new Set(group.map((control) => control.element.getAttribute("href")));
      if (targets.size < 2) continue;
    }
    problems.push(`${group.length} × ${first.role} "${first.name}"`);
  }
  return problems;
}

/**
 * Controls whose aria-label leaves out the text they show (WCAG 2.5.3 Label in Name, axe's
 * label-content-name-mismatch): someone using speech input says what they see ("click RS")
 * and nothing answers. Text hidden only from sight (`sr-only`) is not what they see; text
 * hidden only from assistive technology (`aria-hidden`) is.
 */
export function labelInNameProblems(container: HTMLElement = document.body): string[] {
  const problems: string[] = [];
  for (const control of namedControls(container, ["button", "link", "menuitem"])) {
    const label = control.element.getAttribute("aria-label");
    if (label === null) continue;
    const copy = control.element.cloneNode(true) as HTMLElement;
    copy.querySelectorAll(".sr-only").forEach((node) => node.remove());
    const shown = (copy.textContent ?? "").replace(/\s+/g, " ").trim().toLowerCase();
    // A single character (an icon's letter, "+") is not a label anyone would speak.
    if (shown.length > 1 && !label.toLowerCase().includes(shown)) problems.push(`${control.role} aria-label "${label}" shows "${shown}"`);
  }
  return problems;
}

/** The headings a screen reader lists, in document order, as "h2 Notes". */
export function headingOutline(container: HTMLElement = document.body): string[] {
  return queryAllByRole(container, "heading").map((heading) => `h${headingLevel(heading)} ${heading.textContent?.replace(/\s+/g, " ").trim() ?? ""}`);
}

function headingLevel(heading: HTMLElement): number {
  const aria = Number(heading.getAttribute("aria-level"));
  if (aria) return aria;
  const match = /^H([1-6])$/.exec(heading.tagName);
  return match ? Number(match[1]) : 2; // role="heading" without a level is level 2
}

/**
 * What is wrong with a page's heading outline: not exactly one h1 (`h1: false` for a part of
 * a page, which must have none), a first heading below the h1's level + 1, or a level that
 * skips down (h1 then h3).
 */
export function headingProblems(container: HTMLElement = document.body, { h1 = true }: { h1?: boolean } = {}): string[] {
  const headings = queryAllByRole(container, "heading");
  const levels = headings.map(headingLevel);
  const problems: string[] = [];
  const ones = levels.filter((level) => level === 1).length;
  if (h1 && ones !== 1) problems.push(`expected one h1, found ${ones}`);
  if (!h1 && ones !== 0) problems.push(`expected no h1 in a part of a page, found ${ones}`);
  let previous = h1 ? 0 : 1;
  levels.forEach((level, index) => {
    if (level > previous + 1) problems.push(`h${level} "${headings[index]!.textContent?.trim() ?? ""}" follows h${previous}`);
    previous = level;
  });
  return problems;
}
