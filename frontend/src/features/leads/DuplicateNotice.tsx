"use client";

import { useQuery } from "@tanstack/react-query";
import { CopyCheck } from "lucide-react";
import Link from "next/link";

import { useDebounced } from "@/lib/use-debounced";
import { leadHref, type Workspace } from "@/lib/workspace";

import { leadKeys, leadsApi } from "./api";

const LOOKS_LIKE_EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

/** Full-width digits count, as the server reads them (NFKC). */
export function checkablePhones(phones: readonly string[]): string[] {
  return phones.map((p) => p.trim()).filter((p) => p.normalize("NFKC").replace(/\D/g, "").length >= 5);
}

/**
 * Advisory, while a new opportunity is typed: leads in this workspace with the same phone
 * number or email (the only safe identity: names aren't unique). It never blocks creating
 * and never links the new opportunity to them; a new lead is made regardless (ADR-0028).
 * It only ever looks inside the current workspace, like every other read.
 */
export function DuplicateNotice({ workspace, email, phones }: { workspace: Workspace; email: string; phones: readonly string[] }) {
  const debouncedEmail = useDebounced(LOOKS_LIKE_EMAIL.test(email.trim()) ? email.trim() : "", 600);
  const debouncedPhones = useDebounced(checkablePhones(phones).join("\n"), 600);
  const phoneList = debouncedPhones ? debouncedPhones.split("\n") : [];
  const enabled = Boolean(debouncedEmail || phoneList.length);
  const duplicates = useQuery({
    queryKey: leadKeys.duplicates(workspace, debouncedEmail, phoneList),
    queryFn: () => leadsApi.duplicates(workspace, debouncedEmail, phoneList),
    enabled,
    staleTime: 30_000,
  });
  const found = enabled ? (duplicates.data?.results ?? []) : [];
  return (
    <div aria-live="polite" className="empty:hidden sm:col-span-2">
      {found.length > 0 ? (
        <div className="flex gap-2.5 rounded-md border border-amber-200 bg-amber-50 px-3 py-2.5 text-sm text-amber-900">
          <CopyCheck aria-hidden="true" className="mt-0.5 size-4 shrink-0" />
          <div className="min-w-0">
            <p className="font-medium">{found.length === 1 ? "Possible existing lead" : "Possible existing leads"}</p>
            <ul className="mt-1 space-y-0.5">
              {found.map((lead) => (
                <li key={lead.id} className="[overflow-wrap:anywhere]">
                  <Link href={leadHref(workspace, lead.id)} target="_blank" rel="noopener" className="font-medium underline">
                    {lead.display_name}
                  </Link>
                  <span className="text-amber-800">
                    {" "}
                    · same {lead.matched_on.join(" and ")}
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
