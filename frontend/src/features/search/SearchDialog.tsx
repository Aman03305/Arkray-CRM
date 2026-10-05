"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { type KeyboardEvent, type ReactNode, useEffect, useId, useMemo, useState } from "react";

import { Dialog } from "@/components/ui/Dialog";
import { Spinner } from "@/components/ui/Spinner";
import { statusLabel } from "@/features/activities/ActivityBits";
import { selectedUserId, useWorkspaceSubject } from "@/features/workspace/api";
import { ApiError } from "@/lib/api/client";
import type { SearchResults } from "@/lib/api/types";
import { formatDate, formatDateTime } from "@/lib/format";
import { activityHref, opportunityHref, type Workspace } from "@/lib/workspace";

import { useGlobalSearch } from "./api";
import { Highlight } from "./Highlight";
import { queryState, SEARCH_MAX_LENGTH } from "./query";

/** Typing pauses this long before a request is sent: one request per pause, not per key. */
export const SEARCH_DEBOUNCE_MS = 250;

// No Leads group: there are no lead pages to open (ADR-0027). The API still sends one; a
// customer is found through its opportunities, which match on their customer names too.
type GroupKey = "opportunities" | "tasks" | "meetings" | "notes";

const GROUPS: { key: GroupKey; label: string; one: string }[] = [
  { key: "opportunities", label: "Opportunities", one: "Opportunity" },
  { key: "tasks", label: "Tasks", one: "Task" },
  { key: "meetings", label: "Meetings", one: "Meeting" },
  { key: "notes", label: "Notes", one: "Note" },
];

const OUTCOMES = { open: "Open", won: "Won", lost: "Lost" } as const;
const ELSEWHERE = "Customer in another workspace";

interface Option {
  key: string;
  group: GroupKey;
  href: string;
  title: ReactNode;
  details: ReactNode[];
  /** What a screen reader announces: kind, title and details as plain words. */
  label: string;
}

/** Build an option from plain text parts: shown with the words highlighted, announced as
 * "Opportunity: Analyser upgrade, Apollo Diagnostics, Proposal, Open" (never relying on
 * markup for spacing). */
function option(
  base: { key: string; group: GroupKey; href: string },
  title: string,
  details: string[],
  hl: (text: string) => ReactNode,
  highlighted: readonly number[] = [],
): Option {
  const one = GROUPS.find((g) => g.key === base.group)!.one;
  return {
    ...base,
    title: hl(title),
    details: details.map((detail, i) => (highlighted.includes(i) ? hl(detail) : detail)),
    label: `${one}: ${[title, ...details].join(", ")}`,
  };
}

interface Group {
  key: GroupKey;
  label: string;
  one: string;
  hasMore: boolean;
  options: Option[];
}

/** The customer a task, meeting or note is about (its customer record, the API's `lead`). */
function customerName(lead: { restricted: boolean; display_name?: string }): string {
  return lead.restricted || !lead.display_name ? ELSEWHERE : lead.display_name;
}

/** Whom an opportunity is for: its own account and customer names (what a search may have
 * matched besides the title), else its customer record's name. */
function dealCustomer(deal: SearchResults["opportunities"]["results"][number]): string[] {
  const names = [deal.account_name, deal.customer_name].filter((name, i, all) => name && all.indexOf(name) === i);
  return names.length ? names : [customerName(deal.lead)];
}

/** Every result as an option, in group order. Text only: React escapes all of it. */
function toGroups(data: SearchResults, workspace: Workspace): Group[] {
  const terms = data.terms;
  const owner = (person: { full_name: string }) => (workspace.kind === "organization" ? [person.full_name] : []);
  const hl = (text: string) => <Highlight text={text} terms={terms} />;
  const build: Record<GroupKey, Option[]> = {
    opportunities: data.opportunities.results.map((deal) => {
      const customer = dealCustomer(deal);
      return option(
        { key: `opportunity-${deal.id}`, group: "opportunities", href: opportunityHref(workspace, deal.id) },
        deal.title,
        [
          ...customer,
          deal.stage.name,
          // "Proposal · Open", but just "Won" for the Won stage.
          ...(deal.stage.name === OUTCOMES[deal.status] ? [] : [OUTCOMES[deal.status] ?? deal.status]),
          ...owner(deal.owner),
        ],
        hl,
        customer.map((_, i) => i),
      );
    }),
    tasks: data.tasks.results.map((task) =>
      option(
        { key: `task-${task.id}`, group: "tasks", href: activityHref(workspace, task.id) },
        task.title,
        [
          task.status ? statusLabel(task.status) : "Task",
          task.due_at ? `Due ${formatDate(task.due_at)}` : "No due date",
          ...(task.is_overdue ? ["Overdue"] : []),
          customerName(task.lead),
          ...owner(task.owner),
        ],
        hl,
      ),
    ),
    meetings: data.meetings.results.map((meeting) =>
      option(
        { key: `meeting-${meeting.id}`, group: "meetings", href: activityHref(workspace, meeting.id) },
        meeting.title,
        [
          formatDateTime(meeting.starts_at),
          ...(meeting.location ? [meeting.location] : []),
          meeting.status ? statusLabel(meeting.status) : "Meeting",
          ...(meeting.is_overdue ? ["Awaiting outcome"] : []),
          customerName(meeting.lead),
          ...owner(meeting.owner),
        ],
        hl,
        meeting.location ? [1] : [],
      ),
    ),
    notes: data.notes.results.map((note) => {
      const text = `${note.preview_starts_mid_text ? "…" : ""}${note.preview}${note.preview_truncated ? "…" : ""}`;
      const built = option(
        { key: `note-${note.id}`, group: "notes", href: activityHref(workspace, note.id) },
        text,
        [customerName(note.lead), `Added ${formatDate(note.created_at)}`],
        hl,
      );
      return { ...built, title: <span className="line-clamp-2 [overflow-wrap:anywhere]">{hl(text)}</span> };
    }),
  };
  return GROUPS.map((g) => ({ ...g, hasMore: data[g.key].has_more, options: build[g.key] })).filter(
    (g) => g.options.length > 0,
  );
}

function scopeText(workspace: Workspace, subjectName: string | undefined): string {
  if (workspace.kind === "self") return "Searching your records.";
  if (workspace.kind === "organization") return "Searching all users' records.";
  return subjectName ? `Searching ${subjectName}'s records.` : "Searching the selected user's records.";
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    const field = (error.details as { q?: unknown } | null)?.q;
    if (Array.isArray(field) && typeof field[0] === "string") return field[0];
    return error.message;
  }
  return "Search failed. Please try again.";
}

/**
 * Global search (docs/search.md#frontend): a modal dialog holding a combobox. Focus stays in
 * the input; Up and Down move through the results (announced through
 * aria-activedescendant), Enter or a click opens one in this workspace, Escape closes the
 * dialog and focus returns to whatever opened it. Results are grouped under written-out
 * headings; a polite status line announces what happened.
 *
 * Mounted per workspace by SearchLauncher (keyed by the workspace), and its results are
 * cached under the workspace's key: nothing searched in one workspace can appear in another.
 */
export function SearchDialog({ workspace, onClose }: { workspace: Workspace; onClose: () => void }) {
  const router = useRouter();
  const ids = useId();
  const inputId = `${ids}-input`;
  const listboxId = `${ids}-listbox`;
  const hintId = `${ids}-hint`;
  const optionId = (option: Option) => `${ids}-${option.key}`;

  const [raw, setRaw] = useState("");
  // What is searched: the input after a pause in typing, and never text still being
  // composed with an input method (the candidate isn't chosen yet). Enter flushes it.
  const [debounced, setDebounced] = useState("");
  const [composing, setComposing] = useState(false);
  useEffect(() => {
    if (composing) return;
    const timer = window.setTimeout(() => setDebounced(raw), SEARCH_DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [raw, composing]);
  const now = queryState(raw);
  const typed = now.kind === "ready" ? now.query : null;
  const settled = queryState(debounced);
  const query = typed !== null && settled.kind === "ready" ? settled.query : null;
  const search = useGlobalSearch(workspace, query);

  const subject = useWorkspaceSubject(selectedUserId(workspace));
  const showResults = typed !== null && search.data !== undefined && !search.isError;
  // The results on screen answer exactly what is in the box (not an earlier query's,
  // kept on screen while the next one loads).
  const fresh = showResults && query === typed && !search.isPlaceholderData;
  const groups = useMemo(
    () => (showResults && search.data ? toGroups(search.data, workspace) : []),
    [showResults, search.data, workspace],
  );
  const options = useMemo(() => groups.flatMap((g) => g.options), [groups]);

  // The highlighted option, by key; the first result when it is gone (new results).
  const [activeKey, setActiveKey] = useState<string | null>(null);
  const active = options.find((o) => o.key === activeKey) ?? options[0] ?? null;

  useEffect(() => {
    if (active) document.getElementById(`${ids}-${active.key}`)?.scrollIntoView?.({ block: "nearest" });
  }, [active, ids]);

  const open = (option: Option) => {
    onClose();
    router.push(option.href);
  };

  // Enter typed before the results caught up: search now, and open the first result of
  // *this* query when it arrives (never a result of the previous one still on screen).
  const [pendingOpen, setPendingOpen] = useState<string | null>(null);
  const first = options[0];
  useEffect(() => {
    if (pendingOpen !== null && pendingOpen === typed && fresh && first) {
      onClose();
      router.push(first.href);
    }
  }, [pendingOpen, typed, fresh, first, onClose, router]);

  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    // An input method's own Enter or arrows (229: Safari ends composition first).
    if (event.nativeEvent.isComposing || event.keyCode === 229) return;
    if ((event.key === "ArrowDown" || event.key === "ArrowUp") && options.length > 0) {
      event.preventDefault();
      const index = active ? options.indexOf(active) : -1;
      const step = event.key === "ArrowDown" ? 1 : -1;
      setActiveKey(options[(index + step + options.length) % options.length]!.key);
    } else if (event.key === "Enter") {
      event.preventDefault();
      if (fresh && active) open(active);
      else if (typed !== null) {
        setDebounced(raw);
        setPendingOpen(typed);
      }
    }
  };

  const loading = typed !== null && !showResults && !search.isError;
  const updating = showResults && !fresh;
  let status = "";
  if (now.kind === "invalid") status = now.message;
  else if (typed !== null) {
    if (loading || updating) status = "Searching…";
    else if (showResults && options.length === 0) status = "No matching CRM records";
    else if (showResults) {
      const more = groups.some((g) => g.hasMore);
      status = `${options.length} ${options.length === 1 ? "result" : "results"}${more ? ", more match" : ""}`;
    }
  }

  return (
    <Dialog open title="Search" description={scopeText(workspace, subject.data?.full_name)} onClose={onClose} size="lg">
      <label htmlFor={inputId} className="sr-only">
        Search opportunities, customers, tasks, meetings and notes
      </label>
      <input
        id={inputId}
        data-autofocus
        type="search"
        enterKeyHint="search"
        role="combobox"
        aria-expanded={options.length > 0}
        aria-controls={listboxId}
        aria-autocomplete="list"
        aria-activedescendant={active ? optionId(active) : undefined}
        aria-describedby={hintId}
        aria-invalid={now.kind === "invalid" || undefined}
        autoComplete="off"
        autoCorrect="off"
        spellCheck={false}
        maxLength={SEARCH_MAX_LENGTH}
        value={raw}
        onChange={(event) => {
          setRaw(event.target.value);
          setPendingOpen(null);
        }}
        onCompositionStart={() => setComposing(true)}
        onCompositionEnd={() => setComposing(false)}
        onKeyDown={onKeyDown}
        placeholder="Names, deals, tasks, meetings, notes…"
        className="h-11 w-full rounded-md border border-slate-300 px-3 text-base text-slate-900 placeholder:text-slate-400 focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-500/30 sm:text-sm"
      />
      <p id={hintId} className="mt-2 text-xs text-slate-500">
        {now.kind === "invalid" ? (
          <span className="text-red-700">{now.message}</span>
        ) : (
          "Type a word of at least 3 letters or digits. Every such word must match. Up and Down choose a result, Enter opens it."
        )}
      </p>

      <p role="status" aria-live="polite" className="sr-only">
        {status}
      </p>

      <div className="mt-3" aria-busy={updating || loading || undefined}>
        {loading ? (
          <p className="flex items-center gap-2 px-1 py-6 text-sm text-slate-600">
            <Spinner /> Searching…
          </p>
        ) : null}
        {typed !== null && search.isError ? (
          <div role="alert" className="rounded-md border border-red-200 bg-red-50 px-3 py-3 text-sm text-red-800">
            <p>{errorMessage(search.error)}</p>
            {!(search.error instanceof ApiError && search.error.status === 400) ? (
              <button
                type="button"
                onClick={() => void search.refetch()}
                className="mt-2 rounded-md border border-red-300 bg-white px-2.5 py-1 text-sm font-medium text-red-800 hover:bg-red-50"
              >
                Try again
              </button>
            ) : null}
          </div>
        ) : null}
        {showResults && options.length === 0 ? (
          <p className="px-1 py-6 text-center text-sm text-slate-600">No matching CRM records</p>
        ) : null}
        <div
          role="listbox"
          id={listboxId}
          aria-label="Search results"
          tabIndex={-1}
          hidden={options.length === 0}
          className={`max-h-[min(60vh,32rem)] overflow-y-auto overscroll-contain ${updating ? "opacity-60" : ""}`}
        >
          {groups.map((group) => (
            <div key={group.key} role="group" aria-labelledby={`${ids}-${group.key}-heading`} className="pb-2">
              <div
                role="presentation"
                id={`${ids}-${group.key}-heading`}
                className="sticky top-0 z-10 flex items-baseline justify-between gap-2 bg-white px-2 py-1.5 text-xs font-semibold uppercase tracking-wide text-slate-500"
              >
                <span>{group.label}</span>
                {group.hasMore ? (
                  <span className="font-normal normal-case tracking-normal">Top {group.options.length} shown</span>
                ) : null}
              </div>
              {group.options.map((option) => {
                const selected = option === active;
                return (
                  <Link
                    key={option.key}
                    id={optionId(option)}
                    href={option.href}
                    role="option"
                    aria-selected={selected}
                    aria-label={option.label}
                    tabIndex={-1}
                    prefetch={false}
                    onClick={(event) => {
                      if (event.ctrlKey || event.metaKey || event.shiftKey || event.button !== 0) return;
                      onClose();
                    }}
                    onMouseMove={() => setActiveKey(option.key)}
                    className={`block min-w-0 scroll-mt-9 rounded-md px-2 py-2 text-sm ${
                      selected ? "bg-brand-50 ring-1 ring-inset ring-brand-200" : "hover:bg-slate-50"
                    }`}
                  >
                    <span
                      className={`block min-w-0 font-medium text-slate-900 ${
                        option.group === "notes" ? "font-normal [overflow-wrap:anywhere]" : "truncate"
                      }`}
                    >
                      {option.title}
                    </span>
                    <span className="mt-0.5 block min-w-0 truncate text-xs text-slate-600">
                      {option.details.map((detail, i) => (
                        <span key={i}>
                          {i > 0 ? " · " : null}
                          {detail}
                        </span>
                      ))}
                    </span>
                  </Link>
                );
              })}
            </div>
          ))}
        </div>
      </div>
    </Dialog>
  );
}
