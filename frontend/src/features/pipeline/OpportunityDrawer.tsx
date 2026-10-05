"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, type ReactNode, useEffect, useId, useRef, useState } from "react";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { Drawer } from "@/components/ui/Drawer";
import { SelectField, TextAreaField, TextField } from "@/components/ui/Field";
import { useFocusFirstInvalid } from "@/components/ui/useFocusFirstInvalid";
import { DuplicateNotice } from "@/features/leads/DuplicateNotice";
import { OwnerSelect } from "@/features/users/OwnerSelect";
import { describeError, fieldErrors, isApiError } from "@/lib/api/errors";
import type { CustomField, Opportunity, OpportunityCreateRequest } from "@/lib/api/types";
import { businessToday } from "@/lib/format";
import { formatPercent, parseAmountInput } from "@/lib/money";
import { randomUuid } from "@/lib/random";
import { useViewer } from "@/lib/viewer-context";
import type { Workspace } from "@/lib/workspace";

import { pipelineApi, pipelineKeys } from "./api";
import { CustomFieldInputs } from "./CustomFields";
import {
  changedCustomValues,
  changedFields,
  createRequest,
  customDraft,
  customRequest,
  type CustomValue,
  type Draft,
  draftFromOpportunity,
  EMPTY_DRAFT,
  FIELD_LABELS,
  mergeConflict,
  type Problems,
  updateRequest,
  validateCustom,
  validateDraft,
} from "./draft";
import {
  activeStages,
  defaultPipeline,
  firstOpenStage,
  isNegotiation,
  pipelinePermissions,
  useInstruments,
  useOpportunityWriteSync,
  usePipeline,
  usePipelines,
} from "./hooks";
import { InstrumentPicker } from "./InstrumentPicker";
import { StageName } from "./PipelineBits";

const FORM_ID = "opportunity-drawer-form";
const NO_FIELDS: readonly CustomField[] = [];

interface OpportunityDrawerProps {
  workspace: Workspace;
  /** null: create. */
  opportunity: Opportunity | null;
  /** Create: the pipeline the board shows. */
  pipelineId?: string;
  onClose: () => void;
  onSaved: (opportunity: Opportunity, created: boolean) => void;
}

/**
 * New or edit opportunity, in a panel on the right: the board (or the deal) stays visible
 * behind it on wide screens; full width on phones. Grouped: customer, instrument, timeline,
 * pipeline, additional (the pipeline's custom fields). Nobody names the opportunity or picks
 * a lead: the server names it after its customer and instrument and makes its lead from the
 * customer details, with it, in one transaction (ADR-0028). Organisation-wide its owner is
 * chosen here; elsewhere it is the workspace's user.
 */
export function OpportunityDrawer({ workspace, opportunity, pipelineId: boardPipeline, onClose, onSaved }: OpportunityDrawerProps) {
  const queryClient = useQueryClient();
  const choosesOwner = pipelinePermissions(useViewer(), workspace).choosesOwner;
  const pipelines = usePipelines(workspace);
  const instruments = useInstruments();
  const sync = useOpportunityWriteSync(workspace);
  const editing = opportunity !== null;
  const form = useRef<HTMLFormElement>(null);

  const [base, setBase] = useState<Draft>(() =>
    opportunity ? draftFromOpportunity(opportunity) : { ...EMPTY_DRAFT, opportunity_date: businessToday() },
  );
  const [version, setVersion] = useState(opportunity?.version ?? 0);
  const [draft, setDraft] = useState<Draft>(base);
  const [owner, setOwner] = useState("");
  const [ownerLabel, setOwnerLabel] = useState("");
  const [pipelineId, setPipelineId] = useState(boardPipeline ?? "");
  const [stageId, setStageId] = useState("");
  const [negotiatedPrice, setNegotiatedPrice] = useState("");
  const [manualProbability, setManualProbability] = useState(Boolean(base.probability));
  const [clientErrors, setClientErrors] = useState<Problems>({});
  const [conflict, setConflict] = useState<Opportunity | null>(null);
  const [reloadFailed, setReloadFailed] = useState(false);
  const [archivedMeanwhile, setArchivedMeanwhile] = useState(false);
  const [reviewFields, setReviewFields] = useState<string[]>([]);
  const [confirmDiscard, setConfirmDiscard] = useState(false);
  // One logical create, one key: a double click or a retry of the same form replays the
  // first request (one opportunity, one lead), never makes a second pair.
  const idempotency = useRef<{ body: string; key: string } | null>(null);
  const keyFor = (body: OpportunityCreateRequest): string => {
    const serialised = JSON.stringify(body);
    if (idempotency.current?.body !== serialised) idempotency.current = { body: serialised, key: randomUuid() };
    return idempotency.current.key;
  };

  const usable = (pipelines.data?.results ?? []).filter((p) => p.is_active);
  // Editing: the deal's own pipeline, also when archived (its fields stay editable).
  const own = usePipeline(workspace, editing ? opportunity.pipeline.id : undefined);
  const pipeline = editing ? own.data : (usable.find((p) => p.id === pipelineId) ?? defaultPipeline(usable));
  const stages = activeStages(pipeline);
  const stage = editing ? opportunity.stage : (stages.find((s) => s.id === stageId) ?? firstOpenStage(pipeline));
  const closed = stage ? stage.category !== "open" : false;
  const lost = stage?.category === "lost";
  const fields = pipeline?.custom_fields ?? NO_FIELDS;

  const [customBase, setCustomBase] = useState<Record<string, CustomValue>>({});
  const [custom, setCustom] = useState<Record<string, CustomValue>>({});
  const customFor = useRef<string | null>(null);
  // The custom values follow the pipeline's fields (loaded with the pipelines; a create can
  // switch pipeline): reset whenever the fields shown change.
  useEffect(() => {
    const signature = `${pipeline?.id ?? ""}:${fields.map((f) => f.id).join(",")}`;
    if (customFor.current === signature) return;
    customFor.current = signature;
    const initial = customDraft(fields, editing ? (opportunity.custom_fields as Record<string, unknown>) : undefined);
    setCustomBase(initial);
    setCustom(initial);
  }, [pipeline?.id, fields, editing, opportunity]);

  const dirty =
    changedFields(base, draft).length > 0 ||
    Object.keys(changedCustomValues(customBase, custom)).length > 0 ||
    (!editing && (owner !== "" || stageId !== ""));

  const save = useMutation({
    mutationFn: () => {
      const values = customRequest(fields, custom);
      if (opportunity) {
        return pipelineApi.update(
          workspace,
          opportunity.id,
          updateRequest(base, manualDraft(), version, { before: customRequest(fields, customBase), after: values }),
        );
      }
      const price = parseAmountInput(negotiatedPrice);
      const body = createRequest(manualDraft(), {
        owner: choosesOwner ? owner : undefined,
        pipeline: pipeline?.id,
        stage: stageId || undefined,
        stageProbability: stage?.probability,
        lost,
        negotiatedPrice: isNegotiation(stage) && price.ok ? price.value : undefined,
        customFields: values,
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

  // A new opportunity takes its stage's probability (an own one is set when editing).
  function manualDraft(): Draft {
    return editing && manualProbability && !closed ? draft : { ...draft, probability: "" };
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
    const problems: Problems = {
      ...validateDraft(manualDraft(), { requireOwner: !editing && choosesOwner, owner, creating: !editing }),
      ...validateCustom(fields, custom, !editing),
    };
    // Never created into a pipeline nobody chose: the list must have loaded (review).
    if (!editing && !pipeline) {
      problems.pipeline = [pipelines.isError ? "Pipelines couldn't be loaded. Try again." : "Pipelines are still loading."];
    }
    if (!editing && isNegotiation(stage)) {
      const price = parseAmountInput(negotiatedPrice);
      if (!price.ok) problems.negotiated_price = [negotiatedPrice.trim() ? price.error : "Enter the negotiated price."];
    }
    setClientErrors(problems);
    if (Object.keys(problems).length) return;
    if (editing && changedFields(base, manualDraft()).length === 0 && Object.keys(changedCustomValues(customBase, custom)).length === 0) {
      onClose();
      return;
    }
    setReloadFailed(false);
    save.mutate(undefined, { onSuccess: (saved) => onSaved(saved, !editing) });
  };

  const applyMine = () => {
    if (!conflict) return;
    const latest = draftFromOpportunity(conflict);
    const { merged, overlapping } = mergeConflict(base, manualDraft(), latest);
    setBase(latest);
    setDraft(merged);
    setManualProbability(Boolean(merged.probability));
    setVersion(conflict.version);
    setCustomBase(customDraft(fields, conflict.custom_fields as Record<string, unknown>));
    setReviewFields(overlapping.map((f) => FIELD_LABELS[f]));
    setConflict(null);
    save.reset();
    const first = overlapping[0];
    focusLater(first ? `[name="${first}"], [data-field="${first}"]` : `button[form="${FORM_ID}"]`);
  };

  const discardMine = () => {
    if (!conflict) return;
    const latest = draftFromOpportunity(conflict);
    setBase(latest);
    setDraft(latest);
    setManualProbability(Boolean(latest.probability));
    setVersion(conflict.version);
    const latestCustom = customDraft(fields, conflict.custom_fields as Record<string, unknown>);
    setCustomBase(latestCustom);
    setCustom(latestCustom);
    setReviewFields([]);
    setConflict(null);
    save.reset();
  };

  // An owner error shows under the Owner field organisation-wide; elsewhere there is none (a
  // deactivated user's workspace takes nothing new), so it is the banner's.
  const known = new Set([...Object.keys(EMPTY_DRAFT), ...(choosesOwner ? ["owner"] : []), "stage", "pipeline", "negotiated_price"]);
  const unmapped =
    save.isError && !isApiError(save.error, 409) && !Object.keys(server).some((f) => known.has(f) || f.startsWith("custom_fields"));
  const banner = describeError(save.error);
  // A stray click beside the panel or Escape never throws typing away (enhancement review).
  const close = () => {
    if (save.isPending) return;
    if (dirty && !save.isSuccess) setConfirmDiscard(true);
    else onClose();
  };
  const text = (
    field: keyof Draft,
    maxLength: number,
    { label = FIELD_LABELS[field], ...extra }: { label?: string; optional?: boolean; type?: string; placeholder?: string; inputMode?: "decimal" } = {},
  ) => (
    <TextField
      label={label}
      name={field}
      maxLength={maxLength}
      value={draft[field]}
      onChange={(e) => set(field)(e.target.value)}
      errors={errors[field]}
      autoComplete="off"
      {...extra}
    />
  );

  return (
    <Drawer
      open
      title={editing ? "Edit opportunity" : "New opportunity"}
      description={editing ? opportunity.title : undefined}
      onClose={close}
      busy={save.isPending}
      width="xl"
      footer={
        <>
          <Button variant="secondary" onClick={close} disabled={save.isPending}>
            Cancel
          </Button>
          <Button type="submit" form={FORM_ID} loading={save.isPending}>
            {editing ? "Save changes" : "Create opportunity"}
          </Button>
        </>
      }
    >
      <form id={FORM_ID} ref={form} onSubmit={onSubmit} noValidate className="space-y-5">
        {conflict ? (
          <Alert
            tone="error"
            title="Someone else changed this opportunity"
            action={
              <div className="flex flex-wrap gap-2">
                <Button size="sm" onClick={applyMine} data-conflict-apply>
                  Keep my changes
                </Button>
                <Button size="sm" variant="secondary" onClick={discardMine}>
                  Discard mine
                </Button>
              </div>
            }
          >
            Nothing was saved or overwritten.
          </Alert>
        ) : archivedMeanwhile ? (
          <Alert tone="error" title="This opportunity was archived meanwhile">
            Your changes can&apos;t be saved until it is restored. They are still in the form.
          </Alert>
        ) : isApiError(save.error, 409) ? (
          reloadFailed ? (
            <Alert tone="error">Someone else changed this opportunity and the latest version couldn&apos;t be loaded. Your changes are still here.</Alert>
          ) : (
            <Alert tone="info">Loading the latest version…</Alert>
          )
        ) : unmapped ? (
          <Alert tone="error" requestId={banner.requestId}>
            {(server.non_field_errors ?? server.owner)?.join(" ") ?? banner.message}
          </Alert>
        ) : null}
        {reviewFields.length ? (
          <Alert tone="info" title="Review before saving">
            Also changed by someone else (yours kept): {reviewFields.join(", ")}.
          </Alert>
        ) : null}

        <Group title="Customer">
          {text("account_name", 200)}
          <TextField
            label={FIELD_LABELS.customer_name}
            name="customer_name"
            maxLength={200}
            value={draft.customer_name}
            onChange={(e) => set("customer_name")(e.target.value)}
            errors={errors.customer_name}
            autoComplete="off"
            data-autofocus={editing || undefined}
          />
          {text("contact_phone", 40, { optional: true, type: "tel", placeholder: "Phone number" })}
          {text("contact_email", 254, { optional: true, type: "email" })}
          {editing ? null : <DuplicateNotice workspace={workspace} email={draft.contact_email} phones={[draft.contact_phone]} />}
          <TextAreaField className="sm:col-span-2" label={FIELD_LABELS.address} name="address" optional rows={2} maxLength={1000} value={draft.address} onChange={(e) => set("address")(e.target.value)} errors={errors.address} />
        </Group>

        <Group title="Instrument">
          <InstrumentPicker
            instruments={instruments.names}
            status={instruments.status}
            onRetry={instruments.retry}
            retrying={instruments.retrying}
            value={draft.instrument_name}
            onChange={set("instrument_name")}
            errors={errors.instrument_name}
          />
          {text("work_load", 100, { optional: true, placeholder: "e.g. 300 tests/day" })}
          {text("value", 20, { label: `${FIELD_LABELS.value} (₹)`, inputMode: "decimal" })}
          {text("expected_cpt", 100, { optional: true })}
        </Group>

        <Group title="Timeline">
          <TextField
            label={FIELD_LABELS.opportunity_date}
            name="opportunity_date"
            type="date"
            min="2000-01-01"
            max="2099-12-31"
            value={draft.opportunity_date}
            onChange={(e) => set("opportunity_date")(e.target.value)}
            errors={errors.opportunity_date}
          />
          <TextField
            label={FIELD_LABELS.expected_close_date}
            name="expected_close_date"
            type="date"
            min="2000-01-01"
            max="2099-12-31"
            optional
            value={draft.expected_close_date}
            onChange={(e) => set("expected_close_date")(e.target.value)}
            errors={errors.expected_close_date}
          />
        </Group>

        <Group title="Pipeline">
          {editing ? (
            <p className="text-sm text-slate-700 sm:col-span-2">
              {opportunity.pipeline.name} · <StageName stage={opportunity.stage} />
            </p>
          ) : (
            <>
              {choosesOwner ? (
                <div className="sm:col-span-2">
                  <OwnerSelect
                    label="Owner"
                    name="owner"
                    placeholder="Choose an owner"
                    value={owner}
                    valueLabel={ownerLabel}
                    onChange={(id, label) => {
                      setOwner(id);
                      setOwnerLabel(label);
                    }}
                    errors={errors.owner}
                  />
                </div>
              ) : null}
              {pipelines.isError ? (
                <div role="alert" className="flex flex-wrap items-center gap-2 text-sm text-red-700 sm:col-span-2">
                  Pipelines couldn&apos;t be loaded.
                  <Button variant="secondary" size="sm" onClick={() => void pipelines.refetch()} loading={pipelines.isFetching}>
                    Try again
                  </Button>
                </div>
              ) : null}
              <SelectField
                label="Pipeline"
                name="pipeline"
                value={pipeline?.id ?? ""}
                onChange={(e) => {
                  setPipelineId(e.target.value);
                  setStageId("");
                }}
                options={usable.map((p) => ({ value: p.id, label: p.name }))}
                errors={errors.pipeline}
              />
              <SelectField
                label="Stage"
                name="stage"
                value={stage?.id ?? ""}
                onChange={(e) => setStageId(e.target.value)}
                options={stages.map((s) => ({ value: s.id, label: s.name }))}
                errors={errors.stage}
              />
              {isNegotiation(stage) ? (
                <TextField
                  label="Negotiated price (₹)"
                  name="negotiated_price"
                  inputMode="decimal"
                  value={negotiatedPrice}
                  onChange={(e) => setNegotiatedPrice(e.target.value)}
                  errors={errors.negotiated_price}
                  autoComplete="off"
                />
              ) : null}
            </>
          )}
          {editing && !closed ? (
            <div className="space-y-2 sm:col-span-2">
              <label className="flex items-center gap-2 text-sm text-slate-700">
                <input type="checkbox" checked={manualProbability} onChange={(e) => setManualProbability(e.target.checked)} className="size-4 accent-brand-600" />
                Own probability (stage: {formatPercent(stage?.probability)})
              </label>
              {manualProbability ? (
                <TextField className="sm:max-w-48" label="Probability (%)" name="probability" inputMode="decimal" value={draft.probability} onChange={(e) => set("probability")(e.target.value)} errors={errors.probability} autoComplete="off" />
              ) : null}
            </div>
          ) : null}
          {lost ? (
            <TextAreaField className="sm:col-span-2" label="Lost reason" name="lost_reason" optional rows={2} maxLength={500} value={draft.lost_reason} onChange={(e) => set("lost_reason")(e.target.value)} errors={errors.lost_reason} />
          ) : null}
        </Group>

        {fields.length || editing ? (
          <Group title="Additional">
            <CustomFieldInputs fields={fields} values={custom} onChange={(id, value) => setCustom((c) => ({ ...c, [id]: value }))} errors={errors} />
            {editing ? (
              <TextAreaField className="sm:col-span-2" label="Description" name="description" optional rows={3} maxLength={5000} value={draft.description} onChange={(e) => set("description")(e.target.value)} errors={errors.description} />
            ) : null}
          </Group>
        ) : null}
      </form>
      <ConfirmDialog
        open={confirmDiscard}
        title="Discard your changes?"
        confirmLabel="Discard"
        cancelLabel="Keep editing"
        tone="danger"
        onConfirm={() => {
          setConfirmDiscard(false);
          onClose();
        }}
        onCancel={() => setConfirmDiscard(false)}
      >
        What you typed in this panel isn&apos;t saved.
      </ConfirmDialog>
    </Drawer>
  );
}

function Group({ title, children }: { title: string; children: ReactNode }) {
  const id = useId();
  return (
    <section aria-labelledby={id}>
      <h3 id={id} className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">
        {title}
      </h3>
      <div className="grid gap-x-4 gap-y-3 sm:grid-cols-2">{children}</div>
    </section>
  );
}
