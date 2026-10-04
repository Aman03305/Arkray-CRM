"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import type { MouseEvent } from "react";

import { ActionMenu, type MenuAction } from "@/components/ui/ActionMenu";
import { Skeleton } from "@/components/ui/Skeleton";
import type { LeadListItem } from "@/lib/api/types";
import { formatDate, formatDateTime, formatRelative } from "@/lib/format";
import { leadHref, type Workspace } from "@/lib/workspace";

import { Muted, PersonName, primaryPhone, RatingLabel, StatusBadge } from "./LeadBits";

export type LeadRowAction = "edit" | "archive" | "restore";

interface LeadsTableProps {
  leads: readonly LeadListItem[] | undefined;
  loading: boolean;
  workspace: Workspace;
  showOwner: boolean;
  canWrite: boolean;
  onAction: (action: LeadRowAction, lead: LeadListItem) => void;
}

const TH = "whitespace-nowrap px-4 py-2.5 text-left text-xs font-medium uppercase tracking-wide text-slate-500";
const TD = "px-4 py-3 text-slate-600";

function actionsFor(lead: LeadListItem, canWrite: boolean, onAction: LeadsTableProps["onAction"], open: () => void): MenuAction[] {
  const actions: MenuAction[] = [{ key: "open", label: "Open", onSelect: open }];
  if (!canWrite) return actions;
  if (lead.archived_at) {
    actions.push({ key: "restore", label: "Restore", onSelect: () => onAction("restore", lead) });
  } else {
    actions.push({ key: "edit", label: "Edit", onSelect: () => onAction("edit", lead) });
    actions.push({ key: "archive", label: "Archive", tone: "danger", onSelect: () => onAction("archive", lead) });
  }
  return actions;
}

function When({ iso, relative = false }: { iso: string | null; relative?: boolean }) {
  if (!iso) return <span className="text-slate-500">—</span>;
  return (
    <time dateTime={iso} title={formatDateTime(iso)}>
      {relative ? formatRelative(iso) : formatDate(iso)}
    </time>
  );
}

/**
 * Leads as a table on wider screens (less important columns appear as space allows) and as
 * a card list on phones, rather than a desktop table squeezed until it is unreadable.
 * The lead's name is a real link (keyboard and screen readers); clicking anywhere else on
 * a row opens it too, as a mouse convenience.
 */
export function LeadsTable({ leads, loading, workspace, showOwner, canWrite, onAction }: LeadsTableProps) {
  const router = useRouter();
  const openRow = (lead: LeadListItem) => (event: MouseEvent) => {
    const target = event.target as HTMLElement;
    if (target.closest("a, button, [role='menu']")) return; // the link / menu handle it
    router.push(leadHref(workspace, lead.id));
  };

  if (loading) {
    return (
      <div aria-busy="true" className="space-y-2 rounded-lg border border-slate-200 bg-white p-4">
        {Array.from({ length: 6 }, (_, i) => (
          <Skeleton key={i} className="block h-5 w-full" />
        ))}
        <span className="sr-only">Loading leads</span>
      </div>
    );
  }

  return (
    <>
      {/* Tablet and desktop */}
      {/* Positioned, so its screen-reader-only labels (absolutely positioned) scroll and clip
          with the table: otherwise "Actions" sat beyond the scroll container and made the
          whole page scroll sideways at desktop widths (found in the Phase 7 walkthrough). */}
      <div className="relative hidden overflow-x-auto rounded-lg border border-slate-200 bg-white md:block">
        <table className="min-w-full divide-y divide-slate-200 text-sm">
          <caption className="sr-only">Leads</caption>
          <thead className="bg-slate-50">
            <tr>
              <th scope="col" className={TH}>Lead</th>
              <th scope="col" className={`${TH} hidden lg:table-cell`}>Email</th>
              <th scope="col" className={`${TH} hidden lg:table-cell`}>Phone</th>
              <th scope="col" className={TH}>Status</th>
              <th scope="col" className={`${TH} hidden xl:table-cell`}>Source</th>
              <th scope="col" className={`${TH} hidden xl:table-cell`}>Rating</th>
              {showOwner ? <th scope="col" className={TH}>Owner</th> : null}
              <th scope="col" className={`${TH} hidden lg:table-cell`}>Created</th>
              <th scope="col" className={`${TH} hidden xl:table-cell`}>Last contact</th>
              <th scope="col" className={`${TH} text-right`}>
                <span className="sr-only">Actions</span>
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {leads?.map((lead) => (
              <tr key={lead.id} onClick={openRow(lead)} className="cursor-pointer hover:bg-slate-50/60">
                <td className="max-w-72 px-4 py-3">
                  <Link
                    href={leadHref(workspace, lead.id)}
                    className="block truncate font-medium text-slate-900 hover:text-brand-700 hover:underline"
                  >
                    {lead.display_name}
                  </Link>
                  {lead.organization_name && lead.organization_name !== lead.display_name ? (
                    <span className="block truncate text-xs text-slate-500">{lead.organization_name}</span>
                  ) : null}
                </td>
                <td className={`${TD} hidden max-w-56 truncate lg:table-cell`}>
                  <Muted>{lead.email}</Muted>
                </td>
                <td className={`${TD} hidden whitespace-nowrap lg:table-cell`}>
                  <Muted>{primaryPhone(lead)}</Muted>
                </td>
                <td className="px-4 py-3">
                  <StatusBadge status={lead.status} />
                </td>
                <td className={`${TD} hidden whitespace-nowrap xl:table-cell`}>
                  <Muted>{lead.source?.name}</Muted>
                </td>
                <td className={`${TD} hidden whitespace-nowrap xl:table-cell`}>
                  <RatingLabel rating={lead.rating} />
                </td>
                {showOwner ? (
                  <td className={`${TD} whitespace-nowrap`}>
                    <PersonName person={lead.owner} />
                  </td>
                ) : null}
                <td className={`${TD} hidden whitespace-nowrap lg:table-cell`}>
                  <When iso={lead.created_at} />
                </td>
                <td className={`${TD} hidden whitespace-nowrap xl:table-cell`}>
                  <When iso={lead.last_contacted_at} relative />
                </td>
                <td className="px-4 py-3 text-right">
                  <ActionMenu
                    label={`Actions for ${lead.display_name}`}
                    actions={actionsFor(lead, canWrite, onAction, () => router.push(leadHref(workspace, lead.id)))}
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Phones */}
      <ul aria-label="Leads" className="space-y-2 md:hidden">
        {leads?.map((lead) => (
          <li key={lead.id} className="rounded-lg border border-slate-200 bg-white p-4">
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <Link
                  href={leadHref(workspace, lead.id)}
                  className="block truncate font-medium text-slate-900 hover:text-brand-700 hover:underline"
                >
                  {lead.display_name}
                </Link>
                {lead.organization_name && lead.organization_name !== lead.display_name ? (
                  <p className="truncate text-sm text-slate-500">{lead.organization_name}</p>
                ) : null}
              </div>
              <StatusBadge status={lead.status} />
            </div>
            <dl className="mt-3 grid grid-cols-2 gap-x-3 gap-y-1 text-xs text-slate-600">
              {primaryPhone(lead) ? (
                <>
                  <dt className="sr-only">Phone</dt>
                  <dd className="truncate">{primaryPhone(lead)}</dd>
                </>
              ) : null}
              {lead.rating ? (
                <>
                  <dt className="sr-only">Rating</dt>
                  <dd>
                    <RatingLabel rating={lead.rating} />
                  </dd>
                </>
              ) : null}
              {showOwner ? (
                <>
                  <dt className="sr-only">Owner</dt>
                  <dd className="truncate">
                    <PersonName person={lead.owner} />
                  </dd>
                </>
              ) : null}
              <dt className="sr-only">Created</dt>
              <dd>
                Created <When iso={lead.created_at} />
              </dd>
            </dl>
          </li>
        ))}
      </ul>
    </>
  );
}
