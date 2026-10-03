"use client";

import { useQuery } from "@tanstack/react-query";
import { CopyCheck } from "lucide-react";
import Link from "next/link";

import { leadHref, type Workspace } from "@/lib/workspace";

import { leadKeys, leadsApi } from "./api";
import { useDebounced } from "./hooks";

const LOOKS_LIKE_EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

function checkablePhones(phones: readonly string[]): string[] {
  return phones.map((p) => p.trim()).filter((p) => p.replace(/\D/g, "").length >= 5);
}

/**
 * Advisory: leads in this workspace with the same email or phone number. It never blocks
 * saving and never merges anything (two people can share a switchboard number), and it
 * only ever looks inside the current workspace, like every other read.
 */
export function DuplicateNotice({ workspace, email, phones, excludeId }: {
  workspace: Workspace;
  email: string;
  phones: readonly string[];
  excludeId?: string;
}) {
  const debouncedEmail = useDebounced(LOOKS_LIKE_EMAIL.test(email.trim()) ? email.trim() : "", 600);
  const debouncedPhones = useDebounced(checkablePhones(phones).join("\n"), 600);
  const phoneList = debouncedPhones ? debouncedPhones.split("\n") : [];
  const enabled = Boolean(debouncedEmail || phoneList.length);
  const duplicates = useQuery({
    queryKey: leadKeys.duplicates(workspace, debouncedEmail, phoneList, excludeId ?? null),
    queryFn: () => leadsApi.duplicates(workspace, debouncedEmail, phoneList, excludeId ?? null),
    enabled,
    staleTime: 30_000,
  });
  const found = enabled ? (duplicates.data?.results ?? []) : [];
  return (
    <div aria-live="polite" className="empty:hidden">
      {found.length > 0 ? (
        <div className="flex gap-3 rounded-md border border-amber-200 bg-amber-50 px-3.5 py-3 text-sm text-amber-900">
          <CopyCheck aria-hidden="true" className="mt-0.5 size-4 shrink-0" />
          <div className="min-w-0">
            <p className="font-medium">
              {found.length === 1 ? "A lead with these contact details already exists" : "Leads with these contact details already exist"}
            </p>
            <p className="mt-0.5 text-amber-800">You can still save; nothing is merged.</p>
            <ul className="mt-2 space-y-1">
              {found.map((lead) => (
                <li key={lead.id}>
                  <Link href={leadHref(workspace, lead.id)} target="_blank" rel="noopener" className="font-medium underline">
                    {lead.display_name}
                  </Link>
                  {lead.organization_name && lead.organization_name !== lead.display_name ? `, ${lead.organization_name}` : ""}
                  <span className="text-amber-800">
                    {" "}
                    · same {lead.matched_on.join(" and ")} · owner {lead.owner.full_name}
                    {lead.archived_at ? " · archived" : ""}
                  </span>
                  <span className="sr-only"> (opens in a new tab)</span>
                </li>
              ))}
            </ul>
          </div>
        </div>
      ) : null}
    </div>
  );
}
