"use client";

import { useId, useState } from "react";

import { useDebounced } from "@/lib/use-debounced";

import { NO_MANAGER, SEARCH_MIN_LENGTH, useTeamAdmins } from "./api";

/**
 * The Users list's "Filter by Admin" (a Super admin's, ADR-0035): any Admin, users with no
 * Admin, or one Admin's team, whatever that Admin's status (a deactivated Admin's users stay
 * listed under them until moved). The API's first 100 Admins, shown by name; with more than
 * that a search box beside it narrows the choices server-side.
 *
 * The chosen Admin always stays an option, even once a search hides them or they stop being an
 * Admin (made a User elsewhere): what the select shows is always the filter the list applies.
 */
export function AdminFilter({ value, onChange, className }: {
  /** An Admin's id, NO_MANAGER, or "" for any. */
  value: string;
  onChange: (value: string) => void;
  className: string;
}) {
  const [search, setSearch] = useState("");
  const [picked, setPicked] = useState<{ id: string; label: string } | null>(null);
  const selectId = useId();
  const searchId = useId();
  const q = useDebounced(search.trim().length >= SEARCH_MIN_LENGTH ? search.trim() : "", 300);
  const { query, admins, truncated } = useTeamAdmins(q, true);
  const label = (a: (typeof admins)[number]) => (a.status === "deactivated" ? `${a.full_name} (deactivated)` : a.full_name);
  const kept = value !== "" && value !== NO_MANAGER && !admins.some((a) => a.id === value);

  const choose = (id: string) => {
    const admin = admins.find((a) => a.id === id);
    if (admin) setPicked({ id, label: label(admin) });
    onChange(id);
  };

  return (
    <>
      {truncated ? (
        <div>
          <label htmlFor={searchId} className="sr-only">
            Find an Admin to filter by
          </label>
          <input
            id={searchId}
            type="search"
            value={search}
            maxLength={100}
            placeholder="Find an Admin"
            onChange={(e) => setSearch(e.target.value)}
            className={`${className} w-40`}
          />
        </div>
      ) : null}
      <div>
        <label htmlFor={selectId} className="sr-only">
          Filter by Admin
        </label>
        <select
          id={selectId}
          value={value}
          onChange={(e) => choose(e.target.value)}
          className={className}
          aria-busy={query.isPending || undefined}
        >
          <option value="">Any Admin</option>
          <option value={NO_MANAGER}>No admin</option>
          {kept ? <option value={value}>{picked?.id === value ? picked.label : "Chosen Admin"}</option> : null}
          {admins.map((a) => (
            <option key={a.id} value={a.id}>
              {label(a)}
            </option>
          ))}
        </select>
      </div>
    </>
  );
}
