"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ArrowDown, ArrowUp, Plus, Trash2 } from "lucide-react";
import { type FormEvent, type ReactNode, useId, useRef, useState } from "react";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { Drawer } from "@/components/ui/Drawer";
import { TextField } from "@/components/ui/Field";
import { describeError, fieldErrors, isApiError } from "@/lib/api/errors";
import type { FieldInput, FieldType, PipelineDto, StageInput, StageType } from "@/lib/api/types";
import { parsePercentInput } from "@/lib/money";
import type { Workspace } from "@/lib/workspace";

import { pipelineApi, pipelineKeys } from "./api";

const FORM_ID = "pipeline-settings-form";

const STAGE_TYPES: { value: StageType; label: string }[] = [
  { value: "open", label: "Open" },
  { value: "negotiation", label: "Negotiation" },
  { value: "won", label: "Won" },
  { value: "lost", label: "Lost" },
];

const FIELD_TYPES: { value: FieldType; label: string }[] = [
  { value: "text", label: "Text" },
  { value: "long_text", label: "Long text" },
  { value: "number", label: "Number" },
  { value: "currency", label: "Amount (₹)" },
  { value: "date", label: "Date" },
  { value: "boolean", label: "Yes / no" },
  { value: "single_select", label: "Single choice" },
  { value: "multi_select", label: "Multiple choice" },
];

/** A stage row as the manager edits it (probability as typed). */
interface StageRow {
  key: string; // React key
  id?: string;
  name: string;
  type: StageType;
  probability: string;
}

interface FieldRow {
  key: string;
  id?: string;
  name: string;
  type: FieldType;
  required: boolean;
  /** Choices, one per line (existing ones keep their ids by label). */
  choices: string;
  existingChoices: { id: string; label: string }[];
}

/** New pipelines start from this (all editable). */
export const TEMPLATE: StageRow[] = [
  { key: "t1", name: "New", type: "open", probability: "10" },
  { key: "t2", name: "Qualified", type: "open", probability: "25" },
  { key: "t3", name: "Proposal", type: "open", probability: "50" },
  { key: "t4", name: "Negotiation", type: "negotiation", probability: "75" },
  { key: "t5", name: "Won", type: "won", probability: "100" },
  { key: "t6", name: "Lost", type: "lost", probability: "0" },
];

let counter = 0;
const nextKey = () => `row-${++counter}`;

function rowsOf(pipeline: PipelineDto): StageRow[] {
  return pipeline.stages
    .filter((s) => s.is_active)
    .map((s) => ({ key: s.id, id: s.id, name: s.name, type: s.type, probability: String(Number.parseFloat(s.probability)) }));
}

function fieldsOf(pipeline: PipelineDto): FieldRow[] {
  return pipeline.custom_fields.map((f) => ({
    key: f.id,
    id: f.id,
    name: f.name,
    type: f.type,
    required: f.required,
    choices: f.options.map((o) => o.label).join("\n"),
    existingChoices: f.options.map((o) => ({ id: o.id, label: o.label })),
  }));
}

function stageInput(rows: StageRow[]): StageInput[] {
  return rows.map((row) => {
    const base = { ...(row.id ? { id: row.id } : {}), name: row.name.trim(), type: row.type };
    if (row.type === "won" || row.type === "lost") return base;
    const parsed = parsePercentInput(row.probability);
    return { ...base, probability: parsed.ok ? parsed.value : row.probability.trim() };
  });
}

function fieldInput(rows: FieldRow[]): FieldInput[] {
  return rows.map((row) => {
    const select = row.type === "single_select" || row.type === "multi_select";
    const labels = select ? row.choices.split("\n").map((l) => l.trim()).filter(Boolean) : [];
    const options = labels.map((label) => {
      const known = row.existingChoices.find((c) => c.label === label);
      return known ? { id: known.id, label } : { label };
    });
    return {
      ...(row.id ? { id: row.id } : {}),
      name: row.name.trim(),
      type: row.type,
      required: row.required,
      ...(select ? { options } : {}),
    };
  });
}

/** What can be checked before sending (the server checks everything again). */
function validateConfiguration(name: string, stages: StageRow[], fields: FieldRow[]): Record<string, string[]> {
  const problems: Record<string, string[]> = {};
  if (!name.trim()) problems.name = ["Enter a name."];
  stages.forEach((row, i) => {
    if (!row.name.trim()) problems[`stages[${i}].name`] = ["Enter the stage's name."];
  });
  const seen = new Set<string>();
  fields.forEach((row, i) => {
    const fieldName = row.name.trim();
    if (!fieldName) problems[`fields[${i}].name`] = ["Enter the field's name."];
    else if (seen.has(fieldName.toLowerCase())) problems[`fields[${i}].name`] = ["Another field has this name."];
    seen.add(fieldName.toLowerCase());
    const select = row.type === "single_select" || row.type === "multi_select";
    if (select && !row.choices.split("\n").some((line) => line.trim())) problems[`fields[${i}].options`] = ["Add at least one choice."];
  });
  return problems;
}

/**
 * A pipeline's settings, in a side panel: its name, its stages (add, rename, type,
 * probability, reorder with buttons, remove) and its custom fields; archive. `pipeline`
 * null: a new pipeline, starting from an editable template. Every change is one versioned
 * request; another person's change in between is a conflict, never overwritten.
 */
export function PipelineSettings({
  workspace,
  pipeline,
  onClose,
  onSaved,
}: {
  workspace: Workspace;
  pipeline: PipelineDto | null;
  onClose: () => void;
  onSaved: (pipeline: PipelineDto, message: string) => void;
}) {
  const queryClient = useQueryClient();
  const creating = pipeline === null;
  const [name, setName] = useState(pipeline?.name ?? "");
  const [stages, setStages] = useState<StageRow[]>(() => (pipeline ? rowsOf(pipeline) : TEMPLATE.map((r) => ({ ...r, key: nextKey() }))));
  const [fields, setFields] = useState<FieldRow[]>(() => (pipeline ? fieldsOf(pipeline) : []));
  const [confirmArchive, setConfirmArchive] = useState(false);
  const [live, setLive] = useState("");
  const [clientErrors, setClientErrors] = useState<Record<string, string[]>>({});
  const listRef = useRef<HTMLOListElement>(null);
  // The pipeline as last saved: a save is up to three requests (rename, stages, fields), so
  // after a partial failure the retry continues from what was saved, at its version, never
  // repeating a step (enhancement review: the retry renamed again with a stale version).
  const saved = useRef<PipelineDto | null>(pipeline);

  const refresh = () => void queryClient.invalidateQueries({ queryKey: ["pipeline"] });

  const save = useMutation({
    mutationFn: async () => {
      if (creating) return pipelineApi.createPipeline(workspace, name.trim(), stageInput(stages), fieldInput(fields));
      // Each part only when it differs from what is saved, chaining the version each step
      // returns; each step's result becomes the saved state (and the rows it gave ids to).
      let current = saved.current!;
      if (name.trim() !== current.name) {
        current = saved.current = await pipelineApi.renamePipeline(workspace, current.id, current.version, name.trim());
      }
      if (JSON.stringify(stageInput(stages)) !== JSON.stringify(stageInput(rowsOf(current)))) {
        current = saved.current = await pipelineApi.replaceStages(workspace, current.id, current.version, stageInput(stages));
        setStages(rowsOf(current));
      }
      if (JSON.stringify(fieldInput(fields)) !== JSON.stringify(fieldInput(fieldsOf(current)))) {
        current = saved.current = await pipelineApi.replaceFields(workspace, current.id, current.version, fieldInput(fields));
        setFields(fieldsOf(current));
      }
      return current;
    },
    onSuccess: (saved) => {
      refresh();
      queryClient.setQueryData(pipelineKeys.pipeline(workspace, saved.id), saved);
      onSaved(saved, creating ? `Pipeline “${saved.name}” created.` : "Pipeline saved.");
    },
    onError: () => {
      // Whatever was saved before the failure is shown everywhere else too.
      if (saved.current && saved.current !== pipeline) queryClient.setQueryData(pipelineKeys.pipeline(workspace, saved.current.id), saved.current);
      refresh();
    },
  });

  const archive = useMutation({
    mutationFn: () => pipelineApi.archivePipeline(workspace, saved.current!.id, saved.current!.version),
    onSuccess: (saved) => {
      refresh();
      setConfirmArchive(false);
      onSaved(saved, `Pipeline “${saved.name}” archived.`);
    },
  });

  const server: Record<string, string[]> = { ...fieldErrors(save.error), ...clientErrors };
  const banner = save.isError
    ? isApiError(save.error, 409)
      ? "Someone else changed this pipeline meanwhile. Close and open the settings again to see the latest."
      : server.non_field_errors?.join(" ") ?? (Object.keys(fieldErrors(save.error)).length ? null : describeError(save.error).message)
    : null;

  const moveStage = (index: number, delta: number) => {
    const target = index + delta;
    if (target < 0 || target >= stages.length) return;
    const next = [...stages];
    [next[index], next[target]] = [next[target]!, next[index]!];
    setStages(next);
    setLive(`${next[target]!.name || "Stage"} moved to position ${target + 1} of ${next.length}.`);
    // Keep the keyboard on the same button of the moved row; at the top or bottom that
    // button is disabled (focus would fall to the page), so the row's other one.
    const atEdge = (delta < 0 && target === 0) || (delta > 0 && target === next.length - 1);
    const button = (delta < 0) !== atEdge ? "up" : "down";
    requestAnimationFrame(() => {
      listRef.current?.querySelectorAll<HTMLElement>(`[data-move="${button}"]`)[target]?.focus();
    });
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (save.isPending) return;
    const problems = validateConfiguration(name, stages, fields);
    setClientErrors(problems);
    if (Object.keys(problems).length) {
      save.reset();
      return;
    }
    save.mutate();
  };

  return (
    <Drawer
      open
      title={creating ? "New pipeline" : "Pipeline settings"}
      onClose={() => !save.isPending && onClose()}
      busy={save.isPending || archive.isPending}
      width="lg"
      footer={
        <>
          {!creating && !pipeline.is_default ? (
            <Button variant="ghost" className="sm:mr-auto" onClick={() => setConfirmArchive(true)} disabled={save.isPending}>
              Archive pipeline
            </Button>
          ) : null}
          <Button variant="secondary" onClick={onClose} disabled={save.isPending}>
            Cancel
          </Button>
          <Button type="submit" form={FORM_ID} loading={save.isPending}>
            {creating ? "Create pipeline" : "Save"}
          </Button>
        </>
      }
    >
      <form id={FORM_ID} onSubmit={submit} noValidate className="space-y-6">
        {banner ? (
          <Alert tone="error" requestId={describeError(save.error).requestId}>
            {banner}
          </Alert>
        ) : null}
        <TextField label="Name" name="name" maxLength={100} value={name} onChange={(e) => setName(e.target.value)} errors={server.name} data-autofocus autoComplete="off" />

        <section aria-labelledby="stages-heading">
          <div className="mb-2 flex items-center justify-between">
            <h3 id="stages-heading" className="text-sm font-semibold text-slate-900">
              Stages
            </h3>
            <Button
              size="sm"
              variant="secondary"
              icon={<Plus aria-hidden="true" className="size-4" />}
              disabled={stages.length >= 20}
              onClick={() => {
                const firstClosed = stages.findIndex((s) => s.type === "won" || s.type === "lost");
                const row: StageRow = { key: nextKey(), name: "", type: "open", probability: "50" };
                const at = firstClosed === -1 ? stages.length : firstClosed;
                setStages([...stages.slice(0, at), row, ...stages.slice(at)]);
                requestAnimationFrame(() => listRef.current?.querySelectorAll<HTMLInputElement>("input[data-stage-name]")[at]?.focus());
              }}
            >
              Add stage
            </Button>
          </div>
          {server.stages ? <p className="mb-2 text-sm text-red-600">{server.stages.join(" ")}</p> : null}
          <div aria-live="polite" className="sr-only">
            {live}
          </div>
          <ol ref={listRef} className="space-y-2">
            {stages.map((row, index) => (
              <StageEditor
                key={row.key}
                row={row}
                index={index}
                count={stages.length}
                errors={server}
                onChange={(patch) => setStages(stages.map((r) => (r.key === row.key ? { ...r, ...patch } : r)))}
                onMove={(delta) => moveStage(index, delta)}
                onRemove={() => {
                  setStages(stages.filter((r) => r.key !== row.key));
                  setLive(`${row.name || "Stage"} removed. It is saved when you save.`);
                }}
              />
            ))}
          </ol>
        </section>

        <section aria-labelledby="fields-heading">
          <div className="mb-2 flex items-center justify-between">
            <h3 id="fields-heading" className="text-sm font-semibold text-slate-900">
              Custom fields
            </h3>
            <Button
              size="sm"
              variant="secondary"
              icon={<Plus aria-hidden="true" className="size-4" />}
              disabled={fields.length >= 30}
              onClick={() => setFields([...fields, { key: nextKey(), name: "", type: "text", required: false, choices: "", existingChoices: [] }])}
            >
              Add field
            </Button>
          </div>
          {fields.length === 0 ? <p className="text-sm text-slate-500">None yet.</p> : null}
          <ul className="space-y-2">
            {fields.map((row, index) => (
              <FieldEditor
                key={row.key}
                row={row}
                index={index}
                errors={server}
                onChange={(patch) => setFields(fields.map((r) => (r.key === row.key ? { ...r, ...patch } : r)))}
                onRemove={() => setFields(fields.filter((r) => r.key !== row.key))}
              />
            ))}
          </ul>
        </section>
      </form>
      {pipeline ? (
        <ConfirmDialog
          open={confirmArchive}
          title={`Archive ${pipeline.name}?`}
          confirmLabel="Archive"
          tone="danger"
          busy={archive.isPending}
          error={archive.isError ? describeError(archive.error) : null}
          onConfirm={() => archive.mutate()}
          onCancel={() => setConfirmArchive(false)}
        >
          It disappears from the pipeline list. Its deals and history are kept, and it can be restored.
        </ConfirmDialog>
      ) : null}
    </Drawer>
  );
}

function StageEditor({
  row,
  index,
  count,
  errors,
  onChange,
  onMove,
  onRemove,
}: {
  row: StageRow;
  index: number;
  count: number;
  errors: Record<string, readonly string[] | undefined>;
  onChange: (patch: Partial<StageRow>) => void;
  onMove: (delta: number) => void;
  onRemove: () => void;
}) {
  const ids = { name: useId(), type: useId(), probability: useId(), error: useId() };
  const fixed = row.type === "won" || row.type === "lost";
  const problems = [
    ...(errors[`stages[${index}].name`] ?? []),
    ...(errors[`stages[${index}].probability`] ?? []),
    ...(errors[`stages[${index}].type`] ?? []),
    ...(errors[`stages[${index}].id`] ?? []),
  ];
  const parsed = fixed ? null : parsePercentInput(row.probability);
  const control = "h-9 w-full rounded-md border border-slate-300 bg-white px-2.5 text-sm text-slate-900";
  const label = row.name.trim() || `Stage ${index + 1}`;
  return (
    <li className="rounded-md border border-slate-200 bg-white p-2.5">
      <div className="grid grid-cols-[1fr_auto] gap-2 sm:grid-cols-[minmax(0,1fr)_8.5rem_5.5rem_auto]">
        <div className="col-span-2 sm:col-span-1">
          <label htmlFor={ids.name} className="sr-only">
            Stage {index + 1} name
          </label>
          <input
            id={ids.name}
            data-stage-name
            className={control}
            maxLength={50}
            placeholder="Stage name"
            value={row.name}
            onChange={(e) => onChange({ name: e.target.value })}
            aria-invalid={problems.length ? true : undefined}
            aria-describedby={problems.length ? ids.error : undefined}
          />
        </div>
        <div>
          <label htmlFor={ids.type} className="sr-only">
            {label} type
          </label>
          <select
            id={ids.type}
            className={control}
            value={row.type}
            onChange={(e) => {
              const type = e.target.value as StageType;
              onChange({ type, probability: type === "won" ? "100" : type === "lost" ? "0" : row.probability });
            }}
          >
            {STAGE_TYPES.map((t) => (
              <option key={t.value} value={t.value}>
                {t.label}
              </option>
            ))}
          </select>
        </div>
        <div className="relative">
          <label htmlFor={ids.probability} className="sr-only">
            {label} probability (%)
          </label>
          <input
            id={ids.probability}
            className={`${control} pr-6 disabled:bg-slate-50 disabled:text-slate-500`}
            inputMode="decimal"
            value={fixed ? (row.type === "won" ? "100" : "0") : row.probability}
            disabled={fixed}
            onChange={(e) => onChange({ probability: e.target.value })}
            aria-invalid={parsed && !parsed.ok ? true : undefined}
          />
          <span aria-hidden="true" className="pointer-events-none absolute right-2 top-2 text-sm text-slate-500">
            %
          </span>
        </div>
        <div className="col-span-2 flex justify-end gap-1 sm:col-span-1">
          <IconButton label={`Move ${label} up`} disabled={index === 0} dataMove="up" onClick={() => onMove(-1)}>
            <ArrowUp aria-hidden="true" className="size-4" />
          </IconButton>
          <IconButton label={`Move ${label} down`} disabled={index === count - 1} dataMove="down" onClick={() => onMove(1)}>
            <ArrowDown aria-hidden="true" className="size-4" />
          </IconButton>
          <IconButton label={`Remove ${label}`} disabled={count <= 1} onClick={onRemove} danger>
            <Trash2 aria-hidden="true" className="size-4" />
          </IconButton>
        </div>
      </div>
      {problems.length ? (
        <p id={ids.error} className="mt-1 text-xs text-red-600">
          {problems.join(" ")}
        </p>
      ) : null}
    </li>
  );
}

function FieldEditor({
  row,
  index,
  errors,
  onChange,
  onRemove,
}: {
  row: FieldRow;
  index: number;
  errors: Record<string, readonly string[] | undefined>;
  onChange: (patch: Partial<FieldRow>) => void;
  onRemove: () => void;
}) {
  const ids = { name: useId(), type: useId(), choices: useId(), error: useId() };
  const select = row.type === "single_select" || row.type === "multi_select";
  const problems = Object.entries(errors)
    .filter(([key]) => key.startsWith(`fields[${index}].`))
    .flatMap(([, messages]) => messages ?? []);
  const control = "h-9 w-full rounded-md border border-slate-300 bg-white px-2.5 text-sm text-slate-900 disabled:bg-slate-50";
  const label = row.name.trim() || `Field ${index + 1}`;
  return (
    <li className="rounded-md border border-slate-200 bg-white p-2.5">
      <div className="grid grid-cols-[1fr_auto] gap-2 sm:grid-cols-[minmax(0,1fr)_9.5rem_auto]">
        <div className="col-span-2 sm:col-span-1">
          <label htmlFor={ids.name} className="sr-only">
            Field {index + 1} name
          </label>
          <input
            id={ids.name}
            className={control}
            maxLength={60}
            placeholder="Field name"
            value={row.name}
            onChange={(e) => onChange({ name: e.target.value })}
            aria-invalid={problems.length ? true : undefined}
            aria-describedby={problems.length ? ids.error : undefined}
          />
        </div>
        <div>
          <label htmlFor={ids.type} className="sr-only">
            {label} type
          </label>
          {/* A field's type is fixed once saved (remove it and add another). */}
          <select id={ids.type} className={control} value={row.type} disabled={Boolean(row.id)} onChange={(e) => onChange({ type: e.target.value as FieldType })}>
            {FIELD_TYPES.map((t) => (
              <option key={t.value} value={t.value}>
                {t.label}
              </option>
            ))}
          </select>
        </div>
        <div className="flex items-center justify-end gap-2">
          <label className="flex items-center gap-1.5 text-sm text-slate-700">
            <input type="checkbox" className="size-4 accent-brand-600" checked={row.required} onChange={(e) => onChange({ required: e.target.checked })} />
            Required
          </label>
          <IconButton label={`Remove ${label}`} onClick={onRemove} danger>
            <Trash2 aria-hidden="true" className="size-4" />
          </IconButton>
        </div>
      </div>
      {select ? (
        <div className="mt-2">
          <label htmlFor={ids.choices} className="mb-1 block text-xs font-medium text-slate-600">
            Choices (one per line)
          </label>
          <textarea id={ids.choices} rows={3} className="w-full rounded-md border border-slate-300 px-2.5 py-1.5 text-sm" value={row.choices} onChange={(e) => onChange({ choices: e.target.value })} />
        </div>
      ) : null}
      {problems.length ? (
        <p id={ids.error} className="mt-1 text-xs text-red-600">
          {problems.join(" ")}
        </p>
      ) : null}
    </li>
  );
}

function IconButton({
  label,
  disabled,
  onClick,
  children,
  danger = false,
  dataMove,
}: {
  label: string;
  disabled?: boolean;
  onClick: () => void;
  children: ReactNode;
  danger?: boolean;
  dataMove?: string;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      disabled={disabled}
      data-move={dataMove}
      onClick={onClick}
      className={`inline-flex size-9 items-center justify-center rounded-md border border-slate-200 bg-white disabled:opacity-40 ${
        danger ? "text-red-600 hover:bg-red-50" : "text-slate-600 hover:bg-slate-50"
      }`}
    >
      {children}
    </button>
  );
}
