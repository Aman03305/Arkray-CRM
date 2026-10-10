"use client";

import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, FileArchive, PencilLine } from "lucide-react";
import Link from "next/link";
import { type ReactNode, useId, useState } from "react";

import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { NotFoundView } from "@/components/ui/NotFoundView";
import { PersonName } from "@/components/ui/PersonName";
import { Skeleton } from "@/components/ui/Skeleton";
import { ROW_LINK } from "@/components/ui/targets";
import { pipelinePermissions, usePipelines } from "@/features/pipeline/hooks";
import { Amount, CloseDate, OutcomeBadge } from "@/features/pipeline/PipelineBits";
import { ExportDataDialog } from "@/features/privacy/ExportDataDialog";
import { describeError, isApiError } from "@/lib/api/errors";
import { cursorOf } from "@/lib/api/pagination";
import type { Lead } from "@/lib/api/types";
import { mailtoHref, telHref } from "@/lib/contact-links";
import { businessToday, formatDateTime } from "@/lib/format";
import { hasCapability } from "@/lib/viewer";
import { useViewer } from "@/lib/viewer-context";
import { opportunityHref, sectionBack, type Workspace, workspaceHref } from "@/lib/workspace";

import { leadKeys, leadsApi } from "./api";
import { CorrectDetailsDialog } from "./CorrectDetailsDialog";

/*
 * A lead: the canonical customer record an opportunity is for (ADR-0028). Compact: who the
 * customer is, how to reach them, whose they are, and the opportunity they came with.
 * Everything about the deal lives on the opportunity's own page, one click away. There is no
 * lead form; the one change made here is a correction of the customer's details on their
 * request ("Correct details": the lead and every deal still showing the old value, never
 * the deal's commercial details or history), offered to whoever may write in this workspace,
 * except for a customer whose details were erased. Administrators who handle privacy
 * requests can also export the customer's data. The lead and its opportunities are read
 * through this workspace's API, so a lead of another user's workspace is "not found" here,
 * like any record.
 */

/** What an erased customer's name fields hold (privacy.services.ERASED). */
const ERASED = "[erased]";

type LeadDialog = "correct" | "export" | null;

function Section({ title, children }: { title: ReactNode; children: ReactNode }) {
  const id = useId();
  return (
    <section aria-labelledby={id} className="rounded-lg border border-slate-200 bg-white p-4 sm:p-5">
      <h2 id={id} className="mb-3 text-sm font-semibold text-slate-900">
        {title}
      </h2>
      {children}
    </section>
  );
}

function Fields({ items }: { items: [string, ReactNode][] }) {
  return (
    <dl className="grid gap-x-4 gap-y-2.5 text-sm sm:grid-cols-[8rem_1fr]">
      {items.map(([label, value]) => (
        <div key={label} className="contents">
          <dt className="text-slate-500">{label}</dt>
          <dd className="min-w-0 break-words text-slate-900">{value || <span className="text-slate-400">—</span>}</dd>
        </div>
      ))}
    </dl>
  );
}

function address(lead: Lead): string {
  const locality = [lead.city, lead.state, lead.postal_code].filter(Boolean).join(", ");
  return [lead.address_line_1, lead.address_line_2, locality].filter(Boolean).join("\n");
}

function LeadOpportunities({ workspace, leadId }: { workspace: Workspace; leadId: string }) {
  const [cursor, setCursor] = useState<string | null>(null);
  const pipelines = usePipelines(workspace);
  const list = useQuery({
    queryKey: leadKeys.opportunities(workspace, leadId, cursor),
    queryFn: () => leadsApi.opportunities(workspace, leadId, cursor),
  });
  const stageName = (stageId: string) => pipelines.data?.results.flatMap((p) => p.stages).find((s) => s.id === stageId)?.name ?? "";
  const rows = list.data?.results;
  const paged = Boolean(list.data?.next || list.data?.previous);
  const today = businessToday();
  return (
    <Section title={rows && rows.length > 1 ? "Opportunities" : "Opportunity"}>
      {list.isError ? (
        <p className="text-sm text-red-700">Its opportunities couldn&apos;t be loaded. {describeError(list.error).message}</p>
      ) : !rows ? (
        <Skeleton className="h-12 w-full" />
      ) : rows.length === 0 ? (
        <p className="text-sm text-slate-500">No opportunity in this workspace.</p>
      ) : (
        <ul className="divide-y divide-slate-100">
          {rows.map((row) => (
            // The whole row opens the opportunity (the title alone was a 20 px target).
            <li key={row.id} className="relative py-2.5 first:pt-0 last:pb-0">
              <Link href={opportunityHref(workspace, row.id)} className={`text-sm font-medium text-brand-700 [overflow-wrap:anywhere] hover:underline ${ROW_LINK}`}>
                {row.title}
              </Link>
              <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-slate-600">
                <Amount value={row.value} className="font-medium text-slate-900" />
                {row.status === "open" ? <span>{stageName(row.stage_id)}</span> : <OutcomeBadge status={row.status} />}
                {row.status === "open" ? (
                  <span>
                    Close: <CloseDate date={row.expected_close_date} open today={today} />
                  </span>
                ) : null}
              </div>
            </li>
          ))}
        </ul>
      )}
      {paged ? (
        <nav aria-label="Opportunity pages" className="mt-3 flex justify-end gap-2">
          <Button variant="secondary" size="sm" disabled={!list.data?.previous} onClick={() => setCursor(cursorOf(list.data?.previous))}>
            Previous
          </Button>
          <Button variant="secondary" size="sm" disabled={!list.data?.next} onClick={() => setCursor(cursorOf(list.data?.next))}>
            Next
          </Button>
        </nav>
      ) : null}
    </Section>
  );
}

export function LeadDetailView({ workspace, leadId }: { workspace: Workspace; leadId: string }) {
  const viewer = useViewer();
  const [dialog, setDialog] = useState<LeadDialog>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const detail = useQuery({
    queryKey: leadKeys.detail(workspace, leadId),
    queryFn: () => leadsApi.get(workspace, leadId),
  });
  const back = (
    <Link href={workspaceHref(workspace, "dashboard")} className="-mt-1 mb-2 inline-flex items-center gap-1 py-1 text-sm text-slate-600 hover:text-slate-900">
      <ArrowLeft aria-hidden="true" className="size-4" />
      Dashboard
    </Link>
  );

  if (isApiError(detail.error, 404)) return <NotFoundView back={sectionBack(workspace, "dashboard")} />;
  if (detail.isError && !detail.data) {
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
  if (!lead) {
    return (
      <div aria-busy="true" className="space-y-4">
        <Skeleton className="h-7 w-64" />
        <Skeleton className="h-4 w-40" />
        <Skeleton className="h-32 w-full" />
        <span className="sr-only">Loading lead</span>
      </div>
    );
  }
  const organisation = lead.organization_name && lead.organization_name !== lead.display_name ? lead.organization_name : "";
  const erased = lead.first_name === ERASED || lead.display_name === ERASED;
  const corrects = pipelinePermissions(viewer, workspace).canWrite && !erased;
  // Refused inside a support session: not offered there.
  const exports = hasCapability(viewer, "privacy.manage") && !viewer?.supportSession;
  return (
    <>
      {back}
      <header className="mb-4 flex min-w-0 flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h1 className="min-w-0 break-words text-xl font-semibold tracking-tight text-slate-900">{lead.display_name}</h1>
            <Badge tone="blue">Lead</Badge>
            {lead.archived_at ? <Badge tone="neutral">Archived</Badge> : null}
          </div>
          <p className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-slate-600">
            {organisation ? <span className="min-w-0 [overflow-wrap:anywhere]">{organisation}</span> : null}
            <span>
              <span className="sr-only">Owner: </span>
              <PersonName person={lead.owner} />
            </span>
            <span>
              Created <time dateTime={lead.created_at}>{formatDateTime(lead.created_at)}</time>
            </span>
          </p>
        </div>
        {corrects || exports ? (
          <div className="flex flex-wrap gap-2">
            {corrects ? (
              <Button
                variant="secondary"
                icon={<PencilLine aria-hidden="true" className="size-4" />}
                onClick={() => {
                  setNotice(null);
                  setDialog("correct");
                }}
              >
                Correct details
              </Button>
            ) : null}
            {exports ? (
              <Button
                variant="secondary"
                icon={<FileArchive aria-hidden="true" className="size-4" />}
                onClick={() => {
                  setNotice(null);
                  setDialog("export");
                }}
              >
                Export data
              </Button>
            ) : null}
          </div>
        ) : null}
      </header>
      <div aria-live="polite" className="mb-4 empty:hidden">
        {notice ? <Alert tone="success">{notice}</Alert> : null}
      </div>
      <div className="grid gap-4 lg:grid-cols-2">
        <LeadOpportunities workspace={workspace} leadId={lead.id} />
        <Section title="Contact">
          <Fields
            items={[
              [
                "Phone",
                lead.phone ? (
                  <a key="p" href={telHref(lead.phone)} className="text-brand-700 hover:underline">
                    {lead.phone}
                  </a>
                ) : null,
              ],
              [
                "Email",
                lead.email ? (
                  <a key="e" href={mailtoHref(lead.email)} className="break-all text-brand-700 hover:underline">
                    {lead.email}
                  </a>
                ) : null,
              ],
              ["Address", address(lead) ? <span key="a" className="whitespace-pre-line">{address(lead)}</span> : null],
            ]}
          />
        </Section>
      </div>
      {dialog === "correct" ? (
        <CorrectDetailsDialog
          workspace={workspace}
          lead={lead}
          onClose={() => setDialog(null)}
          onDone={(message) => {
            setDialog(null);
            setNotice(message);
          }}
        />
      ) : null}
      {dialog === "export" ? (
        <ExportDataDialog subject={{ type: "lead", id: lead.id, name: lead.display_name }} onClose={() => setDialog(null)} />
      ) : null}
    </>
  );
}
