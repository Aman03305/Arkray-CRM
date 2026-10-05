"use client";

import { useQuery } from "@tanstack/react-query";
import { useId, useState } from "react";

import { SelectField } from "@/components/ui/Field";
import { useGlobalSearch } from "@/features/search/api";
import { queryState } from "@/features/search/query";
import { describeError } from "@/lib/api/errors";
import type { OpportunityLeadRef } from "@/lib/api/types";
import { useDebounced } from "@/lib/use-debounced";
import type { Workspace } from "@/lib/workspace";

import { pipelineApi, pipelineKeys } from "./api";

interface DealPickerProps {
  workspace: Workspace;
  /** The chosen opportunity's id ("" for none). */
  value: string;
  /** Its label, if known (kept by the caller, e.g. in list filters). */
  valueLabel: string;
  onChange: (opportunityId: string, label: string) => void;
  /** First option, e.g. "Choose an opportunity" or "Any opportunity". */
  placeholder: string;
  errors?: readonly string[];
  hint?: string;
  name?: string;
}

/** "Analyser upgrade · Apollo Diagnostics": the title and whom it is for. */
export function dealLabel(deal: { title: string; lead: OpportunityLeadRef }): string {
  return deal.lead.restricted || !deal.lead.display_name ? deal.title : `${deal.title} · ${deal.lead.display_name}`;
}

/**
 * Choose an opportunity of THIS workspace (ADR-0027: what tasks, meetings and filters are
 * about, now that there are no leads to pick). Before anything is typed, the most recent
 * open ones are listed; typing searches every opportunity of the workspace by title or
 * customer (global search's own rules and scope, so it can't reveal anyone else's). The
 * chosen one always stays listed, so what the select shows is what will be submitted.
 */
export function DealPicker({ workspace, value, valueLabel, onChange, placeholder, errors, hint, name = "opportunity" }: DealPickerProps) {
  const [search, setSearch] = useState("");
  const searchId = useId();
  const state = queryState(useDebounced(search, 300));
  const query = state.kind === "ready" ? state.query : null;
  const recent = useQuery({
    queryKey: pipelineKeys.recentOpen(workspace),
    queryFn: () => pipelineApi.recentOpen(workspace),
    enabled: query === null,
    staleTime: 30_000,
  });
  const found = useGlobalSearch(workspace, query);
  const source = query === null ? recent : found;
  const rows =
    (query === null ? recent.data?.results : found.data?.opportunities.results)?.map((deal) => ({ id: deal.id, label: dealLabel(deal) })) ?? [];
  const options = [
    { value: "", label: source.isPending ? "Loading opportunities…" : query && rows.length === 0 ? "No matching opportunities" : placeholder },
    ...(value && !rows.some((row) => row.id === value) ? [{ value, label: valueLabel || "Selected opportunity" }] : []),
    ...rows.map((row) => ({ value: row.id, label: row.label })),
  ];
  const problem = source.isError ? describeError(source.error).message : state.kind === "invalid" ? state.message : null;
  const more = query !== null && found.data?.opportunities.has_more;
  return (
    <div className="space-y-2">
      <div>
        <label htmlFor={searchId} className="mb-1 block text-xs font-medium text-slate-600">
          Find an opportunity
        </label>
        <input
          id={searchId}
          type="search"
          value={search}
          maxLength={100}
          placeholder="Title, customer or account"
          onChange={(e) => setSearch(e.target.value)}
          className="block h-8 w-full rounded-md border border-slate-300 bg-white px-2.5 text-sm focus-visible:outline-brand-600"
        />
      </div>
      <SelectField
        label="Opportunity"
        name={name}
        value={value}
        onChange={(e) => {
          const row = rows.find((r) => r.id === e.target.value);
          onChange(e.target.value, row ? row.label : e.target.value === value ? valueLabel : "");
        }}
        options={options}
        errors={problem ? [...(errors ?? []), problem] : errors}
        hint={
          more
            ? "Only the best matches are listed: type more of the title or customer."
            : (hint ?? (query === null ? "Recent open opportunities. Type to find any other." : undefined))
        }
        aria-busy={source.isPending || undefined}
      />
    </div>
  );
}
