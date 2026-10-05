"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Contact, Plus } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { EmptyState } from "@/components/ui/EmptyState";
import { NotFoundView } from "@/components/ui/NotFoundView";
import { PageHeader } from "@/components/ui/PageHeader";
import { describeError, isApiError } from "@/lib/api/errors";
import type { LeadListItem } from "@/lib/api/types";
import { useFlash } from "@/lib/flash";
import { useViewer } from "@/lib/viewer-context";
import {
  leadHref,
  newLeadHref,
  type Workspace,
  workspaceApiSegment,
} from "@/lib/workspace";

import { activeFilterCount, cursorOf, effectiveSearch, type LeadFilters, leadKeys, leadsApi } from "./api";
import { leadPermissions, useDebounced, useLeadOptions, useLeadWriteSync } from "./hooks";
import { LeadFiltersBar } from "./LeadFiltersBar";
import { type LeadRowAction, LeadsTable } from "./LeadsTable";
import { useLeadListState } from "./list-state";

type Pending = { action: "archive" | "restore"; lead: LeadListItem } | null;

/**
 * The Leads list, rendered unchanged in every workspace: a salesperson's own (/leads), the
 * organisation for administrators (/leads), and one user's workspace opened by an
 * administrator (/admin/users/{id}/leads). Only the API path differs.
 */
export function LeadsListView({ workspace }: { workspace: Workspace }) {
  const viewer = useViewer();
  const router = useRouter();
  const queryClient = useQueryClient();
  const segment = workspaceApiSegment(workspace);
  const list = useLeadListState(segment);
  const [pending, setPending] = useState<Pending>(null);
  const [notice, setNotice] = useFlash();
  const options = useLeadOptions();
  const permissions = leadPermissions(viewer, workspace);
  const sync = useLeadWriteSync(workspace);
  // Where focus goes when the control that had it disappears (an archived row, a menu).
  // Moved in an effect: it runs after the closing dialog has restored focus to its opener
  // (children's cleanups run first in the same commit), so it always has the last word.
  const noticeRegion = useRef<HTMLDivElement>(null);
  const focusNotice = useRef(false);
  useEffect(() => {
    if (focusNotice.current && notice) {
      focusNotice.current = false;
      noticeRegion.current?.focus();
    }
  }, [notice]);

  const q = useDebounced(list.filters.q, 300);
  const effective: LeadFilters = { ...list.filters, q: effectiveSearch(q) };
  const leads = useQuery({
    queryKey: leadKeys.list(workspace, effective, list.cursor),
    queryFn: () => leadsApi.list(workspace, effective, list.cursor),
    // While a new page or filter loads, keep showing the previous rows, but only from this
    // same workspace: never another user's leads under this user's banner.
    placeholderData: (previous, previousQuery) =>
      previousQuery?.queryKey[2] === segment ? previous : undefined,
  });

  const action = useMutation({
    mutationFn: ({ action: kind, lead }: NonNullable<Pending>) =>
      kind === "archive"
        ? leadsApi.archive(workspace, lead.id, lead.version)
        : leadsApi.restore(workspace, lead.id, lead.version),
    onSuccess: sync,
    onError: (error) => {
      if (isApiError(error, 409) || isApiError(error, 404)) void queryClient.invalidateQueries({ queryKey: leadKeys.lists() });
    },
  });

  const onRowAction = (kind: LeadRowAction, lead: LeadListItem) => {
    setNotice(null);
    if (kind === "edit") router.push(leadHref(workspace, lead.id, "edit"));
    else {
      action.reset();
      setPending({ action: kind, lead });
    }
  };

  if (isApiError(leads.error, 404)) return <NotFoundView />;

  const rows = leads.data?.results;
  const error = leads.isError ? describeError(leads.error) : null;
  const filtered = Boolean(effective.q) || activeFilterCount(list.filters) > 0;
  const subject = workspace.kind === "user" ? "This user" : null;
  const newLead = permissions.canCreate ? (
    <Link
      href={newLeadHref(workspace)}
      className="inline-flex h-9 items-center justify-center gap-2 rounded-full bg-brand-600 px-3.5 text-sm font-medium text-white hover:bg-brand-700"
    >
      <Plus aria-hidden="true" className="size-4" />
      New lead
    </Link>
  ) : null;

  return (
    <>
      <PageHeader title="Leads" actions={newLead} />

      <LeadFiltersBar
        filters={list.filters}
        options={options.data}
        showOwnerFilter={workspace.kind === "organization"}
        onChange={(patch) => {
          setNotice(null);
          list.setFilters(patch);
        }}
        onClear={() => list.resetFilters({ archived: list.filters.archived, ordering: list.filters.ordering })}
      />

      <div ref={noticeRegion} tabIndex={-1} aria-live="polite" className="mb-4 empty:hidden focus:outline-none">
        {notice ? <Alert tone="success">{notice}</Alert> : null}
      </div>

      {error ? (
        <Alert
          tone="error"
          title={isApiError(leads.error, 403) ? "You can't view these leads" : "Leads couldn't be loaded"}
          requestId={error.requestId}
          action={
            isApiError(leads.error, 403) ? null : (
              <Button variant="secondary" size="sm" onClick={() => void leads.refetch()} loading={leads.isFetching}>
                Try again
              </Button>
            )
          }
        >
          {isApiError(leads.error, 400) && list.cursor
            ? "This page link is no longer valid."
            : error.message}
          {isApiError(leads.error, 400) ? (
            <Button variant="secondary" size="sm" className="ml-2" onClick={() => list.resetFilters()}>
              Start over
            </Button>
          ) : null}
        </Alert>
      ) : rows && rows.length === 0 ? (
        list.cursor ? (
          <EmptyState
            icon={Contact}
            title="No more leads"
            action={
              <Button variant="secondary" onClick={() => list.setCursor(null)}>
                Back to the first page
              </Button>
            }
          />
        ) : filtered ? (
          <EmptyState
            icon={Contact}
            title="No leads match your filters"
            description="Try a different search or clear the filters."
            action={
              <Button variant="secondary" onClick={() => list.resetFilters({ archived: list.filters.archived })}>
                Clear filters
              </Button>
            }
          />
        ) : list.filters.archived ? (
          <EmptyState icon={Contact} title="No archived leads" description="Archived leads can be restored." />
        ) : (
          <EmptyState
            icon={Contact}
            title="No leads yet"
            description={
              subject
                ? "This user hasn't added any people or prospects yet."
                : "Add the people and prospects you're working with."
            }
            action={
              permissions.canCreate ? (
                <Link
                  href={newLeadHref(workspace)}
                  aria-label="Add a lead"
                  className="inline-flex h-9 items-center gap-2 rounded-full bg-brand-600 px-3.5 text-sm font-medium text-white hover:bg-brand-700"
                >
                  <Plus aria-hidden="true" className="size-4" />
                  Lead
                </Link>
              ) : null
            }
          />
        )
      ) : (
        <>
          <LeadsTable
            leads={rows}
            loading={leads.isPending}
            workspace={workspace}
            showOwner={workspace.kind === "organization"}
            canWrite={permissions.canWrite}
            onAction={onRowAction}
          />
          <nav aria-label="Pagination" className="mt-4 flex items-center justify-end gap-2">
            <Button
              variant="secondary"
              size="sm"
              disabled={!leads.data?.previous || leads.isFetching}
              onClick={() => list.setCursor(cursorOf(leads.data?.previous))}
            >
              Previous
            </Button>
            <Button
              variant="secondary"
              size="sm"
              disabled={!leads.data?.next || leads.isFetching}
              onClick={() => list.setCursor(cursorOf(leads.data?.next))}
            >
              Next
            </Button>
          </nav>
        </>
      )}

      <ConfirmDialog
        open={pending !== null}
        title={pending?.action === "archive" ? `Archive ${pending.lead.display_name}?` : `Restore ${pending?.lead.display_name ?? ""}?`}
        confirmLabel={pending?.action === "archive" ? "Archive lead" : "Restore lead"}
        tone={pending?.action === "archive" ? "danger" : "primary"}
        busy={action.isPending}
        error={
          action.isError
            ? isApiError(action.error, 409)
              ? { message: "This lead was changed by someone else. The list has been refreshed; try again.", requestId: null }
              : describeError(action.error)
            : null
        }
        onConfirm={() => {
          if (!pending) return;
          // Always the version the list shows now: after a 409 the list is reloaded, and a
          // retry must use the reloaded version, not the one the dialog was opened with.
          const current = leads.data?.results.find((l) => l.id === pending.lead.id);
          if (!current) {
            focusNotice.current = true;
            setPending(null);
            setNotice("That lead is no longer in this list. It may have been changed by someone else.");
            return;
          }
          action.mutate(
            { action: pending.action, lead: current },
            {
              onSuccess: (lead, { action: kind }) => {
                // The row (and the menu that had focus) goes away: focus moves to the notice.
                focusNotice.current = true;
                setPending(null);
                setNotice(kind === "archive" ? `${lead.display_name} was archived.` : `${lead.display_name} was restored.`);
              },
            },
          );
        }}
        onCancel={() => setPending(null)}
      >
        {pending?.action === "archive"
          ? "It will be hidden from the Leads list. Nothing is deleted, and it can be restored from Archived at any time."
          : "It will appear in the Leads list again."}
      </ConfirmDialog>
    </>
  );
}
