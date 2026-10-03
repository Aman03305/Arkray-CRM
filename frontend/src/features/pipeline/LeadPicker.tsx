"use client";

import { useQuery } from "@tanstack/react-query";
import { useId, useState } from "react";

import { SelectField } from "@/components/ui/Field";
import { effectiveSearch, leadKeys, leadsApi, NO_FILTERS, SEARCH_MIN_LENGTH } from "@/features/leads/api";
import { useDebounced } from "@/features/leads/hooks";
import { describeError } from "@/lib/api/errors";
import type { Workspace } from "@/lib/workspace";

interface LeadPickerProps {
  workspace: Workspace;
  value: string;
  valueLabel: string;
  onChange: (leadId: string, label: string) => void;
  errors?: readonly string[];
}

/**
 * Choose the lead an opportunity is for, from THIS workspace's active leads only (the
 * same scoped Leads list, so it can't reveal anyone else's). A search box narrows the
 * list server-side; the chosen lead always stays listed, so what the select shows is what
 * will be submitted.
 */
export function LeadPicker({ workspace, value, valueLabel, onChange, errors }: LeadPickerProps) {
  const [search, setSearch] = useState("");
  const searchId = useId();
  const q = useDebounced(effectiveSearch(search), 300);
  const filters = { ...NO_FILTERS, q, ordering: q ? ("name" as const) : NO_FILTERS.ordering };
  const leads = useQuery({
    queryKey: leadKeys.list(workspace, filters, null),
    queryFn: () => leadsApi.list(workspace, filters, null),
    staleTime: 30_000,
  });
  const rows = leads.data?.results ?? [];
  const label = (lead: { display_name: string; organization_name: string }) =>
    lead.organization_name && lead.organization_name !== lead.display_name ? `${lead.display_name} (${lead.organization_name})` : lead.display_name;
  const options = [
    { value: "", label: leads.isPending ? "Loading leads…" : q && rows.length === 0 ? "No matching leads" : "Choose a lead" },
    ...(value && !rows.some((l) => l.id === value) ? [{ value, label: valueLabel || "Selected lead" }] : []),
    ...rows.map((lead) => ({ value: lead.id, label: label(lead) })),
  ];
  const problem = leads.isError ? describeError(leads.error).message : null;
  return (
    <div className="space-y-2">
      <div>
        <label htmlFor={searchId} className="mb-1 block text-xs font-medium text-slate-600">
          Find a lead
        </label>
        <input
          id={searchId}
          type="search"
          value={search}
          maxLength={100}
          placeholder={`Name, organization, email or phone (at least ${SEARCH_MIN_LENGTH} characters)`}
          onChange={(e) => setSearch(e.target.value)}
          className="block h-8 w-full rounded-md border border-slate-300 bg-white px-2.5 text-sm focus-visible:outline-brand-600"
        />
      </div>
      <SelectField
        label="Lead"
        name="lead"
        value={value}
        onChange={(e) => {
          const lead = rows.find((l) => l.id === e.target.value);
          onChange(e.target.value, lead ? label(lead) : e.target.value === value ? valueLabel : "");
        }}
        options={options}
        errors={problem ? [...(errors ?? []), problem] : errors}
        hint="Only leads in this workspace are listed. The opportunity is owned by the lead's owner."
        aria-busy={leads.isPending || undefined}
      />
    </div>
  );
}
