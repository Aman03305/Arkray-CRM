"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { type FormEvent, useEffect, useRef, useState } from "react";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { SelectField, TextAreaField, TextField } from "@/components/ui/Field";
import { NotFoundView } from "@/components/ui/NotFoundView";
import { PageHeader } from "@/components/ui/PageHeader";
import { Skeleton } from "@/components/ui/Skeleton";
import { useFocusFirstInvalid } from "@/components/ui/useFocusFirstInvalid";
import { leadKeys, leadsApi } from "@/features/leads/api";
import { describeError, fieldErrors, isApiError } from "@/lib/api/errors";
import type { Opportunity, OpportunityCreateRequest } from "@/lib/api/types";
import { setFlash } from "@/lib/flash";
import { formatPercent } from "@/lib/money";
import { randomUuid } from "@/lib/random";
import { useViewer } from "@/lib/viewer-context";
import { opportunityHref, type Workspace, workspaceHref } from "@/lib/workspace";

import { pipelineApi, pipelineKeys } from "./api";
import {
  changedFields,
  createRequest,
  type Draft,
  draftFromOpportunity,
  EMPTY_DRAFT,
  FIELD_LABELS,
  mergeConflict,
  type Problems,
  updateRequest,
  validateDraft,
} from "./draft";
import { activeStages, defaultPipeline, firstOpenStage, pipelinePermissions, useOpportunityWriteSync, usePipelines } from "./hooks";
import { LeadPicker } from "./LeadPicker";
import { StageName } from "./PipelineBits";

type Mode = { kind: "create"; leadId?: string } | { kind: "edit"; opportunityId: string };

/** Create an opportunity, or edit one: the same form in every workspace. */
export function OpportunityFormView({ workspace, mode }: { workspace: Workspace; mode: Mode }) {
  const viewer = useViewer();
  const permissions = pipelinePermissions(viewer, workspace);
  const existing = useQuery({
    queryKey: pipelineKeys.detail(workspace, mode.kind === "edit" ? mode.opportunityId : ""),
    queryFn: () => pipelineApi.get(workspace, mode.kind === "edit" ? mode.opportunityId : ""),
    enabled: mode.kind === "edit",
  });
  if (!permissions.canWrite) return <NotFoundView />;
  if (mode.kind === "create") return <OpportunityForm workspace={workspace} opportunity={null} leadId={mode.leadId} />;
  // Data first: once the form is open, a failed background reload never replaces it.
  if (existing.data) {
    if (existing.data.archived_at) {
      return (
        <Alert
          tone="info"
          title="This opportunity is archived"
          action={
            <Link href={opportunityHref(workspace, existing.data.id)} className="font-medium underline">
              Back to the opportunity
            </Link>
          }
        >
          Restore it before making changes.
        </Alert>
      );
    }
    return <OpportunityForm key={existing.data.id} workspace={workspace} opportunity={existing.data} />;
  }
  if (isApiError(existing.error, 404)) return <NotFoundView />;
  if (existing.isError) {
    const { message, requestId } = describeError(existing.error);
    return (
      <Alert
        tone="error"
        title="This opportunity couldn't be loaded"
        requestId={requestId}
        action={
          <Button variant="secondary" size="sm" onClick={() => void existing.refetch()} loading={existing.isFetching}>
            Try again
          </Button>
        }
      >
        {message}
      </Alert>
    );
  }
  return (
    <div aria-busy="true" className="space-y-4">
      <Skeleton className="h-7 w-56" />
      <Skeleton className="h-64 w-full" />
      <span className="sr-only">Loading opportunity</span>
    </div>
  );
}

function OpportunityForm({ workspace, opportunity, leadId }: { workspace: Workspace; opportunity: Opportunity | null; leadId?: string }) {
  const router = useRouter();
  const queryClient = useQueryClient();
  const pipelines = usePipelines();
  const sync = useOpportunityWriteSync(workspace);
  const editing = opportunity !== null;
  const form = useRef<HTMLFormElement>(null);

  const [base, setBase] = useState<Draft>(() => (opportunity ? draftFromOpportunity(opportunity) : EMPTY_DRAFT));
  const [version, setVersion] = useState(opportunity?.version ?? 0);
  const [draft, setDraft] = useState<Draft>(base);
  const [lead, setLead] = useState(leadId ?? "");
  const [leadLabel, setLeadLabel] = useState("");
  const [pipelineId, setPipelineId] = useState("");
  const [stageId, setStageId] = useState("");
  const [manualProbability, setManualProbability] = useState(Boolean(base.probability));
  const [clientErrors, setClientErrors] = useState<Problems>({});
  const [conflict, setConflict] = useState<Opportunity | null>(null);
  const [reloadFailed, setReloadFailed] = useState(false);
  const [archivedMeanwhile, setArchivedMeanwhile] = useState(false);
  const [reviewFields, setReviewFields] = useState<string[]>([]);
  // A create retried with exactly the same body reuses its key (a timeout can't create two
  // opportunities); any difference is a new request with a new key.
  const idempotency = useRef<{ body: string; key: string } | null>(null);
  const keyFor = (body: OpportunityCreateRequest): string => {
    const serialised = JSON.stringify(body);
    if (idempotency.current?.body !== serialised) idempotency.current = { body: serialised, key: randomUuid() };
    return idempotency.current.key;
  };

  // A lead given in the URL (from its page): show its name.
  const presetLead = useQuery({
    queryKey: leadKeys.detail(workspace, leadId ?? ""),
    queryFn: () => leadsApi.get(workspace, leadId ?? ""),
    enabled: Boolean(leadId) && !editing,
  });
  const shownLeadLabel = leadLabel || (presetLead.data && presetLead.data.id === lead ? presetLead.data.display_name : "");

  const allPipelines = (pipelines.data?.results ?? []).filter((p) => p.is_active);
  const pipeline = editing
    ? pipelines.data?.results.find((p) => p.id === opportunity.pipeline.id)
    : (allPipelines.find((p) => p.id === pipelineId) ?? defaultPipeline(allPipelines));
  const stages = activeStages(pipeline);
  const stage = editing ? opportunity.stage : (stages.find((s) => s.id === stageId) ?? firstOpenStage(pipeline));
  const closed = stage ? stage.category !== "open" : false;
  const lost = stage?.category === "lost";

  const dirty = changedFields(base, draft).length > 0 || (!editing && (lead !== (leadId ?? "") || stageId !== ""));

  const save = useMutation({
    mutationFn: () => {
      if (opportunity) return pipelineApi.update(workspace, opportunity.id, updateRequest(base, manualDraft(), version));
      const body = createRequest(manualDraft(), {
        lead,
        pipeline: allPipelines.length > 1 ? pipeline?.id : undefined,
        stage: stageId || undefined,
        stageProbability: stage?.probability,
        lost,
      });
      return pipelineApi.create(workspace, body, keyFor(body));
    },
    onSuccess: sync,
    onError: async (error) => {
      if (!opportunity || !isApiError(error, 409)) return;
      setReloadFailed(false);
      try {
        const latest = await pipelineApi.get(workspace, opportunity.id);
        if (latest.archived_at) {
          // Don't hand the page the archived version: it would replace this form (and the
          // unsaved typing) with "archived" (review). Say so here instead.
          setArchivedMeanwhile(true);
          return;
        }
        queryClient.setQueryData(pipelineKeys.detail(workspace, opportunity.id), latest);
        setConflict(latest);
      } catch {
        setConflict(null);
        setReloadFailed(true);
      }
    },
  });

  /** The draft as submitted: a probability only while "set manually" is on. */
  function manualDraft(): Draft {
    return manualProbability && !closed ? draft : { ...draft, probability: "" };
  }

  useEffect(() => {
    if (conflict) form.current?.querySelector<HTMLElement>("[data-conflict-apply]")?.focus();
  }, [conflict]);

  useEffect(() => {
    if (!dirty || save.isPending || save.isSuccess) return;
    const warn = (event: BeforeUnloadEvent) => event.preventDefault();
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty, save.isPending, save.isSuccess]);

  const server = fieldErrors(save.error);
  const errors: Record<string, readonly string[] | undefined> = { ...server, ...clientErrors };
  useFocusFirstInvalid(form, Object.keys(clientErrors).length ? clientErrors : save.error);
  const set = (field: keyof Draft) => (value: string) => setDraft((d) => ({ ...d, [field]: value }));
  const focusLater = (selector: string) => requestAnimationFrame(() => form.current?.querySelector<HTMLElement>(selector)?.focus());

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    if (save.isPending) return;
    if (conflict) {
      focusLater("[data-conflict-apply]");
      return;
    }
    const problems = validateDraft(manualDraft(), { requireLead: !editing, lead });
    setClientErrors(problems);
    if (Object.keys(problems).length) return;
    if (editing && changedFields(base, manualDraft()).length === 0) {
      router.push(opportunityHref(workspace, opportunity.id));
      return;
    }
    setReloadFailed(false);
    save.mutate(undefined, {
      onSuccess: (saved) => {
        setFlash(opportunity ? "Changes saved." : "Opportunity created.");
        router.push(opportunityHref(workspace, saved.id));
      },
    });
  };

  const applyMine = () => {
    if (!conflict) return;
    const latest = draftFromOpportunity(conflict);
    const { merged, overlapping } = mergeConflict(base, manualDraft(), latest);
    setBase(latest);
    setDraft(merged);
    setManualProbability(Boolean(merged.probability));
    setVersion(conflict.version);
    setReviewFields(overlapping.map((f) => FIELD_LABELS[f]));
    setConflict(null);
    save.reset();
    focusLater(overlapping.length ? `[name="${overlapping[0]}"]` : 'button[type="submit"]');
  };

  const discardMine = () => {
    if (!conflict) return;
    const latest = draftFromOpportunity(conflict);
    setBase(latest);
    setDraft(latest);
    setManualProbability(Boolean(latest.probability));
    setVersion(conflict.version);
    setReviewFields([]);
    setConflict(null);
    save.reset();
    focusLater('button[type="submit"]');
  };

  const cancelHref = opportunity ? opportunityHref(workspace, opportunity.id) : workspaceHref(workspace, "pipeline");
  const known = new Set(["title", "value", "probability", "expected_close_date", "description", "lost_reason", "lead", "stage", "pipeline"]);
  const unmapped = save.isError && !isApiError(save.error, 409) && !Object.keys(server).some((f) => known.has(f));
  const banner = describeError(save.error);

  return (
    <>
      <Link href={cancelHref} className="mb-4 inline-flex items-center gap-1 text-sm text-slate-600 hover:text-slate-900">
        <ArrowLeft aria-hidden="true" className="size-4" />
        {opportunity ? opportunity.title : "Pipeline"}
      </Link>
      <PageHeader
        title={opportunity ? "Edit opportunity" : "New opportunity"}
        subtitle={opportunity ? undefined : "An opportunity is a potential sale to one of your leads. It is owned by the lead's owner."}
      />

      <form ref={form} onSubmit={onSubmit} noValidate className="max-w-3xl space-y-4">
        {conflict ? (
          <Alert
            tone="error"
            title="Someone else changed this opportunity while you were editing"
            action={
              <div className="flex flex-wrap gap-2">
                <Button size="sm" onClick={applyMine} data-conflict-apply>
                  Apply my changes to the latest version
                </Button>
                <Button size="sm" variant="secondary" onClick={discardMine}>
                  Discard my changes
                </Button>
              </div>
            }
          >
            Your changes haven&apos;t been saved yet. Nothing was overwritten.
          </Alert>
        ) : archivedMeanwhile ? (
          <Alert tone="error" title="Someone archived this opportunity while you were editing">
            Your changes haven&apos;t been saved and can&apos;t be: it must be restored first. They are still in the form, so copy anything
            you need before leaving.
          </Alert>
        ) : isApiError(save.error, 409) ? (
          reloadFailed ? (
            <Alert tone="error">
              Someone else changed this opportunity, and its latest version couldn&apos;t be loaded here. Your changes are still in the form,
              not saved. Copy anything you need, then go back and try again.
            </Alert>
          ) : (
            <Alert tone="info">Someone else changed this opportunity. Loading the latest version…</Alert>
          )
        ) : unmapped ? (
          <Alert tone="error" requestId={banner.requestId}>
            {server.non_field_errors?.join(" ") ?? banner.message}
          </Alert>
        ) : null}
        {reviewFields.length ? (
          <Alert tone="info" title="Review before saving">
            Your changes were applied to the latest version. These fields were also changed by someone else; your values are kept:{" "}
            {reviewFields.join(", ")}.
          </Alert>
        ) : null}

        <section aria-label="Opportunity" className="grid gap-4 rounded-lg border border-slate-200 bg-white p-5 sm:grid-cols-2">
          {editing ? (
            <p className="text-sm text-slate-600 sm:col-span-2">
              Lead: <strong className="font-medium text-slate-900">{opportunity.lead.restricted ? "In another workspace" : opportunity.lead.display_name}</strong>
              {" · "}
              Stage: <strong className="font-medium text-slate-900"><StageName stage={opportunity.stage} /></strong>
              <span className="block text-xs text-slate-500">To change the stage, use Move on the opportunity or the pipeline.</span>
            </p>
          ) : (
            <div className="sm:col-span-2">
              <LeadPicker
                workspace={workspace}
                value={lead}
                valueLabel={shownLeadLabel}
                onChange={(id, label) => {
                  setLead(id);
                  setLeadLabel(label);
                }}
                errors={errors.lead}
              />
            </div>
          )}
          <TextField
            className="sm:col-span-2"
            label="Title"
            name="title"
            value={draft.title}
            maxLength={200}
            onChange={(e) => set("title")(e.target.value)}
            errors={errors.title}
            hint="What is being sold, e.g. Hospital Lab Analyzer Upgrade."
            autoComplete="off"
          />
          <TextField
            label="Value (₹)"
            name="value"
            inputMode="decimal"
            value={draft.value}
            onChange={(e) => set("value")(e.target.value)}
            errors={errors.value}
            hint="In rupees, e.g. 12,50,000 or 1250000.50."
            autoComplete="off"
          />
          <TextField
            label="Expected close date"
            name="expected_close_date"
            type="date"
            min="2000-01-01"
            max="2099-12-31"
            optional
            value={draft.expected_close_date}
            onChange={(e) => set("expected_close_date")(e.target.value)}
            errors={errors.expected_close_date}
          />
          {!editing && allPipelines.length > 1 ? (
            <SelectField
              label="Pipeline"
              name="pipeline"
              value={pipeline?.id ?? ""}
              onChange={(e) => {
                setPipelineId(e.target.value);
                setStageId("");
              }}
              options={allPipelines.map((p) => ({ value: p.id, label: p.name }))}
              errors={errors.pipeline}
            />
          ) : null}
          {!editing ? (
            <SelectField
              label="Stage"
              name="stage"
              value={stage?.id ?? ""}
              onChange={(e) => setStageId(e.target.value)}
              options={stages.map((s) => ({ value: s.id, label: `${s.name} (${formatPercent(s.probability)})` }))}
              errors={errors.stage}
            />
          ) : null}
          <div className="sm:col-span-2">
            {closed ? (
              <p className="text-sm text-slate-600">
                Probability: <strong className="font-medium text-slate-900">{formatPercent(stage?.probability)}</strong>{" "}
                <span className="text-slate-500">(fixed for {stage?.category} opportunities)</span>
              </p>
            ) : (
              <fieldset className="space-y-2">
                <legend className="text-sm font-medium text-slate-700">Probability</legend>
                <label className="flex items-center gap-2 text-sm text-slate-700">
                  <input
                    type="checkbox"
                    checked={manualProbability}
                    onChange={(e) => setManualProbability(e.target.checked)}
                    className="size-4 accent-brand-600"
                  />
                  Set the probability manually (otherwise the stage&apos;s {formatPercent(stage?.probability)})
                </label>
                {manualProbability ? (
                  <TextField
                    label="Probability (%)"
                    name="probability"
                    inputMode="decimal"
                    value={draft.probability}
                    onChange={(e) => set("probability")(e.target.value)}
                    errors={errors.probability}
                    hint="0 to 100. Moving to another stage replaces it with that stage's default."
                    autoComplete="off"
                  />
                ) : null}
              </fieldset>
            )}
          </div>
          {lost ? (
            <TextAreaField
              className="sm:col-span-2"
              label="Lost reason"
              name="lost_reason"
              optional
              rows={2}
              maxLength={500}
              value={draft.lost_reason}
              onChange={(e) => set("lost_reason")(e.target.value)}
              errors={errors.lost_reason}
            />
          ) : null}
          <TextAreaField
            className="sm:col-span-2"
            label="Description"
            name="description"
            optional
            rows={5}
            maxLength={5000}
            value={draft.description}
            onChange={(e) => set("description")(e.target.value)}
            errors={errors.description}
            hint="Needs, scope, context. Up to 5,000 characters."
          />
        </section>

        <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
          <Link
            href={cancelHref}
            className="inline-flex h-9 items-center justify-center rounded-md border border-slate-300 bg-white px-3.5 text-sm font-medium text-slate-700 hover:bg-slate-50"
          >
            Cancel
          </Link>
          <Button type="submit" loading={save.isPending}>
            {opportunity ? "Save changes" : "Create opportunity"}
          </Button>
        </div>
      </form>
    </>
  );
}
