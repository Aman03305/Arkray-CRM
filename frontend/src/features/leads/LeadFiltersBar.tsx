"use client";

import { Search, SlidersHorizontal } from "lucide-react";
import { type ReactNode, useId, useRef, useState } from "react";

import { Button } from "@/components/ui/Button";
import type { LeadOptions, LeadOrdering, Rating } from "@/lib/api/types";

import { activeFilterCount, type LeadFilters, ORDERING_OPTIONS, SEARCH_MAX_LENGTH, SEARCH_MIN_LENGTH } from "./api";
import { OwnerSelect } from "./OwnerSelect";

const CONTROL =
  "h-9 rounded-md border border-slate-300 bg-white px-3 text-sm text-slate-900 focus-visible:outline-brand-600";

interface LeadFiltersBarProps {
  filters: LeadFilters;
  options: LeadOptions | undefined;
  showOwnerFilter: boolean;
  onChange: (patch: Partial<LeadFilters>) => void;
  onClear: () => void;
}

function Labelled({ id, label, children }: { id: string; label: string; children: ReactNode }) {
  return (
    <div>
      <label htmlFor={id} className="mb-1 block text-xs font-medium text-slate-600">
        {label}
      </label>
      {children}
    </div>
  );
}

/**
 * Search, sort, the Active/Archived view and the allowlisted filters. Only filters the API
 * accepts exist here; the owner filter only in the organisation-wide workspace.
 */
export function LeadFiltersBar({ filters, options, showOwnerFilter, onChange, onClear }: LeadFiltersBarProps) {
  const ids = {
    search: useId(),
    status: useId(),
    sort: useId(),
    source: useId(),
    rating: useId(),
    from: useId(),
    to: useId(),
    panel: useId(),
  };
  const searchInput = useRef<HTMLInputElement>(null);
  const count = activeFilterCount(filters);
  const [expanded, setExpanded] = useState(count > 0);
  const tooShort = filters.q.trim().length > 0 && filters.q.trim().length < SEARCH_MIN_LENGTH;

  return (
    <div className="mb-4 space-y-3">
      <div role="group" aria-label="Show" className="inline-flex rounded-md border border-slate-300 bg-white p-0.5 text-sm">
        {[
          { archived: false, label: "Active leads" },
          { archived: true, label: "Archived" },
        ].map((tab) => (
          <button
            key={tab.label}
            type="button"
            aria-pressed={filters.archived === tab.archived}
            onClick={() => onChange({ archived: tab.archived })}
            className={`rounded px-3 py-1 ${
              filters.archived === tab.archived ? "bg-slate-900 font-medium text-white" : "text-slate-600 hover:bg-slate-50"
            }`}
          >
            {tab.label}
          </button>
        ))}
      </div>

      <div role="search" className="flex flex-wrap items-end gap-3">
        <div className="relative min-w-56 flex-1 sm:max-w-sm">
          <label htmlFor={ids.search} className="sr-only">
            Search leads
          </label>
          <Search aria-hidden="true" className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-slate-500" />
          <input
            ref={searchInput}
            id={ids.search}
            type="search"
            placeholder="Search name, organization, email or phone"
            maxLength={SEARCH_MAX_LENGTH}
            value={filters.q}
            onChange={(e) => onChange({ q: e.target.value })}
            aria-describedby={tooShort ? `${ids.search}-hint` : undefined}
            className={`${CONTROL} w-full pl-9`}
          />
        </div>
        <div>
          <label htmlFor={ids.status} className="sr-only">
            Filter by status
          </label>
          <select id={ids.status} value={filters.status} onChange={(e) => onChange({ status: e.target.value })} className={CONTROL}>
            <option value="">All statuses</option>
            {options?.statuses.map((s) => (
              <option key={s.key} value={s.key}>
                {s.name}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label htmlFor={ids.sort} className="sr-only">
            Sort by
          </label>
          <select
            id={ids.sort}
            value={filters.ordering}
            onChange={(e) => onChange({ ordering: e.target.value as LeadOrdering })}
            className={CONTROL}
          >
            {ORDERING_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </div>
        <Button
          variant="secondary"
          icon={<SlidersHorizontal aria-hidden="true" className="size-4" />}
          aria-expanded={expanded}
          aria-controls={ids.panel}
          onClick={() => setExpanded((v) => !v)}
        >
          More filters{count > 0 ? ` (${count})` : ""}
        </Button>
        {count > 0 || filters.q ? (
          <Button
            variant="ghost"
            onClick={() => {
              onClear();
              searchInput.current?.focus(); // this button disappears; keep focus in the bar
            }}
          >
            Clear
          </Button>
        ) : null}
      </div>
      {tooShort ? (
        <p id={`${ids.search}-hint`} className="text-xs text-slate-500">
          Type at least {SEARCH_MIN_LENGTH} characters to search.
        </p>
      ) : null}

      <div id={ids.panel} hidden={!expanded} className="rounded-lg border border-slate-200 bg-white p-4">
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <Labelled id={ids.source} label="Source">
            <select id={ids.source} value={filters.source} onChange={(e) => onChange({ source: e.target.value })} className={`${CONTROL} w-full`}>
              <option value="">Any source</option>
              {options?.sources.map((s) => (
                <option key={s.key} value={s.key}>
                  {s.name}
                </option>
              ))}
            </select>
          </Labelled>
          <Labelled id={ids.rating} label="Rating">
            <select
              id={ids.rating}
              value={filters.rating}
              onChange={(e) => onChange({ rating: e.target.value as Rating | "" })}
              className={`${CONTROL} w-full`}
            >
              <option value="">Any rating</option>
              {options?.ratings.map((r) => (
                <option key={r.key} value={r.key}>
                  {r.name}
                </option>
              ))}
            </select>
          </Labelled>
          <Labelled id={ids.from} label="Created from">
            <input
              id={ids.from}
              type="date"
              value={filters.createdFrom}
              max={filters.createdTo || undefined}
              onChange={(e) => onChange({ createdFrom: e.target.value })}
              className={`${CONTROL} w-full`}
            />
          </Labelled>
          <Labelled id={ids.to} label="Created to">
            <input
              id={ids.to}
              type="date"
              value={filters.createdTo}
              min={filters.createdFrom || undefined}
              onChange={(e) => onChange({ createdTo: e.target.value })}
              className={`${CONTROL} w-full`}
            />
          </Labelled>
          {showOwnerFilter ? (
            <div className="sm:col-span-2">
              <OwnerSelect
                label="Owner"
                placeholder="Anyone"
                value={filters.owner}
                valueLabel={filters.ownerLabel}
                onChange={(owner, ownerLabel) => onChange({ owner, ownerLabel })}
              />
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}
