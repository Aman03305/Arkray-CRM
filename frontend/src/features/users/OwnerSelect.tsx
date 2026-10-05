"use client";

import { useQuery } from "@tanstack/react-query";
import { useId, useState } from "react";

import { SelectField } from "@/components/ui/Field";
import { describeError } from "@/lib/api/errors";
import { useDebounced } from "@/lib/use-debounced";

import { ASSIGNEE_SEARCH_MIN_LENGTH, assigneeKeys, assigneesApi } from "./assignees";

interface OwnerSelectProps {
  label: string;
  /** The chosen user's id ("" for none). */
  value: string;
  /** The chosen user's name, if known (kept by the caller, e.g. in list filters). */
  valueLabel?: string;
  onChange: (userId: string, label: string) => void;
  errors?: readonly string[];
  hint?: string;
  /** First option, e.g. "Choose an owner" or "Anyone". */
  placeholder: string;
  autoFocus?: boolean;
  /** The form field name (server errors are focused by it). */
  name?: string;
}

/**
 * Pick an active user who can own CRM records (GET /api/v1/assignees, crm.assign_any).
 * A native select, so it is keyboard- and screen-reader-friendly everywhere. The first 100
 * users by name are listed; with more than that a search box narrows the list server-side.
 *
 * The chosen user always stays in the list, even when a search hides them, so what the
 * select shows is always exactly what will be submitted (Phase 2 review).
 */
export function OwnerSelect({
  label,
  value,
  valueLabel,
  onChange,
  errors,
  hint,
  placeholder,
  autoFocus,
  name,
}: OwnerSelectProps) {
  const [search, setSearch] = useState("");
  const [picked, setPicked] = useState<{ id: string; label: string } | null>(null);
  const searchId = useId();
  const q = useDebounced(search.trim().length >= ASSIGNEE_SEARCH_MIN_LENGTH ? search.trim() : "", 300);
  const assignees = useQuery({
    queryKey: assigneeKeys.list(q),
    queryFn: () => assigneesApi.list(q),
    staleTime: 60_000,
  });
  const users = assignees.data?.results ?? [];
  const truncated = Boolean(assignees.data?.next) || q !== "";
  const optionLabel = (u: { full_name: string; email: string }) => `${u.full_name} (${u.email})`;
  const selectedLabel = picked?.id === value ? picked.label : valueLabel || "Selected user";
  const options = [
    { value: "", label: assignees.isPending ? "Loading users…" : placeholder },
    ...(value && !users.some((u) => u.id === value) ? [{ value, label: selectedLabel }] : []),
    ...users.map((u) => ({ value: u.id, label: optionLabel(u) })),
  ];
  const problem = assignees.isError ? describeError(assignees.error).message : null;

  const choose = (id: string) => {
    const user = users.find((u) => u.id === id);
    const text = user ? optionLabel(user) : id === value ? selectedLabel : "";
    setPicked(id ? { id, label: text } : null);
    onChange(id, id ? text : "");
  };

  return (
    <div className="space-y-2">
      {truncated ? (
        <div>
          <label htmlFor={searchId} className="mb-1 block text-xs font-medium text-slate-600">
            Find a user
          </label>
          <input
            id={searchId}
            type="search"
            value={search}
            maxLength={100}
            placeholder="Name or email"
            onChange={(e) => setSearch(e.target.value)}
            className="block h-8 w-full rounded-md border border-slate-300 bg-white px-2.5 text-sm focus-visible:outline-brand-600"
          />
        </div>
      ) : null}
      <SelectField
        label={label}
        name={name}
        value={value}
        onChange={(e) => choose(e.target.value)}
        options={options}
        errors={problem ? [...(errors ?? []), problem] : errors}
        hint={hint}
        aria-busy={assignees.isPending || undefined}
        data-autofocus={autoFocus || undefined}
      />
    </div>
  );
}
