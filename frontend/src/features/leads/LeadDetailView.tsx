"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Archive, ArrowLeft, Pencil, Repeat } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { type ReactNode, useEffect, useRef, useState } from "react";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { NotFoundView } from "@/components/ui/NotFoundView";
import { Skeleton } from "@/components/ui/Skeleton";
import { CurrentWork } from "@/features/activities/CurrentWork";
import { NoteComposer, Timeline } from "@/features/activities/Timeline";
import { ConvertLeadDialog } from "@/features/pipeline/ConvertLeadDialog";
import { LeadOpportunities } from "@/features/pipeline/LeadOpportunities";
import { describeError, isApiError } from "@/lib/api/errors";
import type { Lead } from "@/lib/api/types";
import { setFlash, useFlash } from "@/lib/flash";
import { formatDateTime, formatRelative } from "@/lib/format";
import { useViewer } from "@/lib/viewer-context";
import { leadHref, opportunityHref, type Workspace, workspaceHref } from "@/lib/workspace";

import { leadKeys, leadsApi } from "./api";
import { countryName, leadPermissions, leftBehind, movedOutOf, useLeadOptions, useLeadWriteSync } from "./hooks";
import { ChangeStatusDialog, ReassignDialog } from "./LeadActionDialogs";
import { PersonName, RatingLabel, StatusBadge, telHref } from "./LeadBits";

type OpenDialog = "status" | "reassign" | "archive" | "restore" | "convert" | null;

function Section({ title, children, className = "" }: { title: string; children: ReactNode; className?: string }) {
  return (
    <section aria-label={title} className={`rounded-lg border border-slate-200 bg-white p-5 ${className}`}>
      <h2 className="mb-3 text-sm font-semibold text-slate-900">{title}</h2>
      {children}
    </section>
  );
}

function Fields({ items }: { items: [string, ReactNode][] }) {
  return (
    <dl className="grid gap-x-4 gap-y-3 text-sm sm:grid-cols-[10rem_1fr]">
      {items.map(([label, value]) => (
        <div key={label} className="contents">
          <dt className="text-slate-500">{label}</dt>
          <dd className="min-w-0 break-words text-slate-900">{value || <span className="text-slate-400">—</span>}</dd>
        </div>
      ))}
    </dl>
  );
}

function When({ iso }: { iso: string | null }) {
  if (!iso) return null;
  return (
    <time dateTime={iso} title={formatDateTime(iso)}>
      {formatDateTime(iso)} <span className="text-slate-500">({formatRelative(iso)})</span>
    </time>
  );
}

function Phone({ value }: { value: string }) {
  if (!value) return null;
  // The link is what dialers understand (extension as ";ext="); the text is as entered.
  return (
    <a href={telHref(value)} className="text-brand-700 hover:underline">
      {value}
    </a>
  );
}

function DetailSkeleton() {
  return (
    <div aria-busy="true" className="space-y-4">
      <Skeleton className="h-7 w-64" />
      <Skeleton className="h-4 w-40" />
      <div className="grid gap-4 lg:grid-cols-2">
        <Skeleton className="h-40 w-full" />
        <Skeleton className="h-40 w-full" />
      </div>
      <span className="sr-only">Loading lead</span>
    </div>
  );
}

export function LeadDetailView({ workspace, leadId }: { workspace: Workspace; leadId: string }) {
  const viewer = useViewer();
  const router = useRouter();
  const queryClient = useQueryClient();
  const options = useLeadOptions();
  const [dialog, setDialog] = useState<OpenDialog>(null);
  const [notice, setNotice] = useFlash();
  const permissions = leadPermissions(viewer, workspace);
  // Set when a reassignment moves the lead out of the workspace being viewed: its cached
  // copy under this workspace, with its timeline and open work there, is dropped once this
  // page is gone (dropping them while the page is still mounted would refetch them here,
  // where the lead no longer exists).
  const movedOut = useRef(false);
  useEffect(
    () => () => {
      if (!movedOut.current) return;
      queryClient.removeQueries({ queryKey: leadKeys.detail(workspace, leadId) });
      const left = leftBehind(workspace, leadId);
      queryClient.removeQueries({ predicate: (query) => left.includes(JSON.stringify(query.queryKey)) });
    },
    [queryClient, workspace, leadId],
  );
  const detail = useQuery({
    queryKey: leadKeys.detail(workspace, leadId),
    queryFn: () => leadsApi.get(workspace, leadId),
  });

  const sync = useLeadWriteSync(workspace);
  const lifecycle = useMutation({
    mutationFn: (kind: "archive" | "restore") =>
      kind === "archive"
        ? leadsApi.archive(workspace, leadId, detail.data?.version ?? 0)
        : leadsApi.restore(workspace, leadId, detail.data?.version ?? 0),
    onSuccess: sync,
    onError: (error) => {
      if (isApiError(error, 409)) void queryClient.invalidateQueries({ queryKey: leadKeys.detail(workspace, leadId) });
    },
  });

  // Caches are synced by the mutations themselves; this only updates the page.
  function done(message: string) {
    setDialog(null);
    setNotice(message);
  }

  function reassigned(lead: Lead) {
    // Reassigned out of the workspace being viewed (a user's, or one's own): it lives in the
    // new owner's workspace now, so this page can't show it any more.
    if (movedOutOf(workspace, lead, viewer?.id)) {
      movedOut.current = true;
      setFlash(`${lead.display_name} was reassigned to ${lead.owner.full_name}. It now appears in their workspace.`);
      router.push(workspaceHref(workspace, "leads"));
      return;
    }
    done(`Reassigned to ${lead.owner.full_name}.`);
  }

  const back = (
    <Link href={workspaceHref(workspace, "leads")} className="mb-4 inline-flex items-center gap-1 text-sm text-slate-600 hover:text-slate-900">
      <ArrowLeft aria-hidden="true" className="size-4" />
      Leads
    </Link>
  );

  if (isApiError(detail.error, 404)) return <NotFoundView />;
  if (detail.isError) {
    const { message, requestId } = describeError(detail.error);
    return (
      <>
        {back}
        <Alert
          tone="error"
          title={isApiError(detail.error, 403) ? "You can't view this lead" : "This lead couldn't be loaded"}
          requestId={requestId}
          action={
            isApiError(detail.error, 403) ? null : (
              <Button variant="secondary" size="sm" onClick={() => void detail.refetch()} loading={detail.isFetching}>
                Try again
              </Button>
            )
          }
        >
          {message}
        </Alert>
      </>
    );
  }
  const lead = detail.data;
  if (!lead) return <DetailSkeleton />;

  const archived = lead.archived_at !== null;
  const canEdit = permissions.canWrite && !archived;
  const address = [lead.address_line_1, lead.address_line_2, [lead.city, lead.state, lead.postal_code].filter(Boolean).join(", "), countryName(lead.country)]
    .filter(Boolean)
    .join("\n");

  return (
    <>
      {back}
      <header className="mb-6 flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <h1 className="break-words text-xl font-semibold tracking-tight text-slate-900">{lead.display_name}</h1>
          <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-2 text-sm text-slate-600">
            <span className="inline-flex items-center gap-2">
              <span className="sr-only">Status:</span>
              <StatusBadge status={lead.status} />
              {canEdit ? (
                <button type="button" onClick={() => setDialog("status")} className="text-sm font-medium text-brand-700 hover:underline">
                  Change status
                </button>
              ) : null}
            </span>
            <span className="inline-flex items-center gap-1">
              Owner: <strong className="font-medium text-slate-900"><PersonName person={lead.owner} /></strong>
              {permissions.canAssign && permissions.canWrite && !archived ? (
                <button type="button" onClick={() => setDialog("reassign")} className="ml-1 text-sm font-medium text-brand-700 hover:underline">
                  Reassign
                </button>
              ) : null}
            </span>
          </div>
        </div>
        {permissions.canWrite ? (
          <div className="flex items-center gap-2">
            {archived ? (
              <Button variant="secondary" onClick={() => { lifecycle.reset(); setDialog("restore"); }}>
                Restore
              </Button>
            ) : (
              <>
                {lead.status.category !== "converted" ? (
                  <Button variant="secondary" icon={<Repeat aria-hidden="true" className="size-4" />} onClick={() => setDialog("convert")}>
                    Convert
                  </Button>
                ) : null}
                <Button variant="ghost" icon={<Archive aria-hidden="true" className="size-4" />} onClick={() => { lifecycle.reset(); setDialog("archive"); }}>
                  Archive
                </Button>
                <Link
                  href={leadHref(workspace, lead.id, "edit")}
                  className="inline-flex h-9 items-center gap-2 rounded-md bg-brand-600 px-3.5 text-sm font-medium text-white hover:bg-brand-700"
                >
                  <Pencil aria-hidden="true" className="size-4" />
                  Edit
                </Link>
              </>
            )}
          </div>
        ) : null}
      </header>

      <div aria-live="polite" className="mb-4 empty:hidden">
        {notice ? <Alert tone="success">{notice}</Alert> : null}
      </div>
      {archived ? (
        <div className="mb-4">
          <Alert tone="info" title="This lead is archived">
            Archived {formatDateTime(lead.archived_at)}. It is hidden from the Leads list{permissions.canWrite ? "; restore it to make changes" : ""}.
          </Alert>
        </div>
      ) : null}

      <div className="grid gap-4 lg:grid-cols-3">
        <div className="space-y-4 lg:col-span-2">
          <Section title="Contact">
            <Fields
              items={[
                ["Email", lead.email ? <a href={`mailto:${lead.email}`} className="text-brand-700 hover:underline">{lead.email}</a> : null],
                ["Phone", <Phone key="p" value={lead.phone} />],
                ["Mobile", <Phone key="m" value={lead.mobile} />],
                ["Alternate phone", <Phone key="a" value={lead.alternate_phone} />],
              ]}
            />
          </Section>
          <Section title="Organization and role">
            <Fields items={[["Organization", lead.organization_name], ["Job title", lead.job_title]]} />
          </Section>
          <Section title="Address">
            {address ? <p className="whitespace-pre-line text-sm text-slate-900">{address}</p> : <p className="text-sm text-slate-400">No address</p>}
          </Section>
          <Section title="Description">
            {lead.description ? (
              <p className="whitespace-pre-line break-words text-sm text-slate-900">{lead.description}</p>
            ) : (
              <p className="text-sm text-slate-400">No description</p>
            )}
          </Section>
          <Timeline
            workspace={workspace}
            subject={{ kind: "lead", id: lead.id }}
            composer={canEdit ? <NoteComposer workspace={workspace} link={{ lead: lead.id }} /> : null}
          />
        </div>
        <div className="space-y-4">
          <Section title="Sales information">
            <Fields
              items={[
                ["Status", lead.status.name],
                ["Source", lead.source?.name],
                ["Rating", lead.rating ? <RatingLabel rating={lead.rating} /> : null],
                ["Last contacted", <When key="c" iso={lead.last_contacted_at} />],
              ]}
            />
          </Section>
          <LeadOpportunities workspace={workspace} leadId={lead.id} canCreate={permissions.canWrite && !archived} />
          <CurrentWork workspace={workspace} target={{ lead: lead.id }} label={lead.display_name} canWrite={canEdit} />
          <Section title="Record details">
            <Fields
              items={[
                ["Created by", <PersonName key="cb" person={lead.created_by} />],
                ["Created", <When key="ca" iso={lead.created_at} />],
                ["Last updated", <When key="ua" iso={lead.updated_at} />],
              ]}
            />
          </Section>
        </div>
      </div>

      {dialog === "status" ? (
        <ChangeStatusDialog
          workspace={workspace}
          lead={lead}
          options={options.data}
          onClose={() => setDialog(null)}
          onDone={(updated) => done(`Status changed to ${updated.status.name}.`)}
        />
      ) : null}
      {dialog === "convert" ? (
        <ConvertLeadDialog
          workspace={workspace}
          lead={lead}
          onClose={() => setDialog(null)}
          onConverted={(result) => {
            setFlash(`${result.lead.display_name} was converted. This is the new opportunity.`);
            router.push(opportunityHref(workspace, result.opportunity.id));
          }}
        />
      ) : null}
      {dialog === "reassign" ? (
        <ReassignDialog workspace={workspace} lead={lead} onClose={() => setDialog(null)} onDone={reassigned} />
      ) : null}
      <ConfirmDialog
        open={dialog === "archive" || dialog === "restore"}
        title={dialog === "archive" ? `Archive ${lead.display_name}?` : `Restore ${lead.display_name}?`}
        confirmLabel={dialog === "archive" ? "Archive lead" : "Restore lead"}
        tone={dialog === "archive" ? "danger" : "primary"}
        busy={lifecycle.isPending}
        error={
          lifecycle.isError
            ? isApiError(lifecycle.error, 409)
              ? { message: "Someone else changed this lead a moment ago. The latest details are shown now; please try again.", requestId: null }
              : describeError(lifecycle.error)
            : null
        }
        onConfirm={() => {
          const kind = dialog === "archive" ? "archive" : "restore";
          lifecycle.mutate(kind, { onSuccess: () => done(kind === "archive" ? "Lead archived." : "Lead restored.") });
        }}
        onCancel={() => setDialog(null)}
      >
        {dialog === "archive"
          ? "It will be hidden from the Leads list. Nothing is deleted, and it can be restored at any time."
          : "It will appear in the Leads list again and can be edited."}
      </ConfirmDialog>
    </>
  );
}
