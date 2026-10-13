"use client";

import { useId, useState } from "react";

import { SelectField } from "@/components/ui/Field";
import { describeError } from "@/lib/api/errors";
import type { AdminUser } from "@/lib/api/types";
import { useDebounced } from "@/lib/use-debounced";

import { SEARCH_MIN_LENGTH, useTeamAdmins } from "./api";

export const NO_ADMIN_LABEL = "No admin (managed by Super admins)";

interface ManagerSelectProps {
  /** The chosen Admin's id ("" for none). */
  value: string;
  /** The chosen Admin's name, if known (e.g. the user's current Admin). */
  valueLabel?: string;
  onChange: (adminId: string, label: string) => void;
  errors?: readonly string[];
  /** The user being edited: never their own Admin (an Admin being made a User is still listed
   * as an Admin until the change is saved; the API refuses it). */
  excludeId?: string;
}

/** An Admin as an option: name and email (two Admins may share a name), and if still invited. */
export function adminOptionLabel(admin: Pick<AdminUser, "full_name" | "email" | "status">): string {
  return `${admin.full_name} (${admin.email}${admin.status === "invited" ? ", invited" : ""})`;
}

/**
 * A sales user's Admin: the team administrator who manages them (ADR-0035), or none (then
 * only the Super admins manage them). Super admins only; the API checks the choice again (an
 * active or invited Admin). A native select, like OwnerSelect: the API's first 100 Admins
 * (newest first), shown by name; with more than that a search box narrows the list
 * server-side. The chosen Admin always stays in the list, so what it shows is exactly what
 * will be sent.
 */
export function ManagerSelect({ value, valueLabel, onChange, errors, excludeId }: ManagerSelectProps) {
  const [search, setSearch] = useState("");
  const searchId = useId();
  const q = useDebounced(search.trim().length >= SEARCH_MIN_LENGTH ? search.trim() : "", 300);
  const { query, admins, truncated } = useTeamAdmins(q, true);
  // A deactivated or pseudonymised Admin takes no one new (the API refuses it), nor does the
  // user themselves.
  const choices = admins.filter((a) => a.status !== "deactivated" && !a.pseudonymised_at && a.id !== excludeId);
  const options = [
    { value: "", label: NO_ADMIN_LABEL },
    ...(value && !choices.some((a) => a.id === value) ? [{ value, label: valueLabel || "Their current Admin" }] : []),
    ...choices.map((a) => ({ value: a.id, label: adminOptionLabel(a) })),
  ];
  const problem = query.isError ? describeError(query.error).message : null;

  const choose = (id: string) => {
    const admin = choices.find((a) => a.id === id);
    onChange(id, admin ? admin.full_name : id === value ? (valueLabel ?? "") : "");
  };

  return (
    <div className="space-y-2">
      {truncated ? (
        <div>
          <label htmlFor={searchId} className="mb-1 block text-xs font-medium text-slate-600">
            Find an Admin
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
        label="Admin"
        name="manager"
        value={value}
        onChange={(e) => choose(e.target.value)}
        options={options}
        errors={problem ? [...(errors ?? []), problem] : errors}
        hint="Their Admin manages their account and can open their CRM."
        aria-busy={query.isPending || undefined}
      />
    </div>
  );
}
