"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useId, useState } from "react";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Dialog, DialogActions } from "@/components/ui/Dialog";
import { TextField } from "@/components/ui/Field";
import { PersonName } from "@/components/ui/PersonName";
import { Skeleton } from "@/components/ui/Skeleton";
import { describeError, fieldErrors, isApiError } from "@/lib/api/errors";
import type { Opportunity } from "@/lib/api/types";
import { formatDateOnly, formatDateTime } from "@/lib/format";
import { amountInputValue, formatInr, parseAmountInput, sameDecimal } from "@/lib/money";
import type { Workspace } from "@/lib/workspace";

import { type CorrectedField, type OpportunityCorrection, type OpportunityCorrectionRequest, pipelineApi, pipelineKeys } from "./api";

/**
 * Correcting a won or lost deal's figures (adversarial review of 73ff1e3, M2). No edit changes
 * a closed deal's value, probability or dates (the server answers 422 `deal_closed`): reopen it,
 * or an administrator corrects them here, with a reason. Every correction is appended to the
 * deal's corrections (old and new values, who, when, why), shown on its History tab. The API
 * asks for a recent second factor (StepUpProvider prompts and repeats the request).
 */

export const CORRECTION_LABELS: Record<CorrectedField, string> = {
  value: "Installation price",
  expected_close_date: "Expected closing date",
  opportunity_date: "Opportunity date",
};
const REASON_MAX_LENGTH = 300;
const NOTHING_CHANGED = "Change the installation price or a date to correct.";
const REASON_REQUIRED = "Say why this deal is corrected.";

/** A figure as a correction shows it: an amount in rupees, a date, or "none". */
function figure(field: CorrectedField, value: string | null): string {
  if (value === null || value === "") return "none";
  return field === "value" ? formatInr(value) : formatDateOnly(value);
}

type CorrectionForm = { value: string; opportunity_date: string; expected_close_date: string; reason: string };
type CorrectionProblems = Partial<Record<keyof CorrectionForm | "non_field_errors", string[]>>;

/** The figures that differ from the deal's, as the API takes them; problems with what was typed. */
export function correctionRequest(
  opportunity: Pick<Opportunity, "value" | "opportunity_date" | "expected_close_date" | "version">,
  form: CorrectionForm,
): { body: OpportunityCorrectionRequest | null; problems: CorrectionProblems } {
  const problems: CorrectionProblems = {};
  const body: { -readonly [K in keyof OpportunityCorrectionRequest]: OpportunityCorrectionRequest[K] } = {
    version: opportunity.version,
    reason: form.reason.trim(),
  };
  const amount = parseAmountInput(form.value);
  if (!amount.ok) problems.value = [amount.error];
  else if (!sameDecimal(amount.value, opportunity.value)) body.value = amount.value;
  if (!form.opportunity_date) problems.opportunity_date = ["Enter the opportunity date."];
  else if (form.opportunity_date !== opportunity.opportunity_date) body.opportunity_date = form.opportunity_date;
  if ((form.expected_close_date || null) !== (opportunity.expected_close_date ?? null)) body.expected_close_date = form.expected_close_date || null;
  if (!body.reason) problems.reason = [REASON_REQUIRED];
  if (!Object.keys(problems).length && body.value === undefined && body.opportunity_date === undefined && body.expected_close_date === undefined) {
    problems.non_field_errors = [NOTHING_CHANGED];
  }
  return { body: Object.keys(problems).length ? null : body, problems };
}

export function CorrectionDialog({
  workspace,
  opportunity,
  onClose,
  onSaved,
}: {
  workspace: Workspace;
  opportunity: Opportunity;
  onClose: () => void;
  onSaved: (opportunity: Opportunity) => void;
}) {
  const queryClient = useQueryClient();
  const [form, setForm] = useState<CorrectionForm>(() => ({
    value: amountInputValue(opportunity.value),
    opportunity_date: opportunity.opportunity_date,
    expected_close_date: opportunity.expected_close_date ?? "",
    reason: "",
  }));
  const [problems, setProblems] = useState<CorrectionProblems>({});
  const save = useMutation({
    mutationFn: (body: OpportunityCorrectionRequest) => pipelineApi.correct(workspace, opportunity.id, body),
    onSuccess: onSaved,
    // Someone changed the deal meanwhile: load it, so a retry sends its new version (and is
    // compared with its latest figures).
    onError: (failure) => {
      if (isApiError(failure, 409)) void queryClient.invalidateQueries({ queryKey: pipelineKeys.detail(workspace, opportunity.id) });
    },
  });
  const set = (field: keyof CorrectionForm) => (value: string) => {
    setForm((f) => ({ ...f, [field]: value }));
    setProblems((p) => ({ ...p, [field]: undefined, non_field_errors: undefined }));
  };
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (save.isPending) return;
    const { body, problems: found } = correctionRequest(opportunity, form);
    setProblems(found);
    if (body) save.mutate(body);
  };
  const server = fieldErrors(save.error);
  const known = ["value", "opportunity_date", "expected_close_date", "reason", "non_field_errors"];
  const banner = save.isError && !Object.keys(server).some((f) => known.includes(f)) ? describeError(save.error) : null;
  const general = problems.non_field_errors ?? server.non_field_errors;
  return (
    <Dialog
      open
      title="Correct this deal"
      description={
        <p>
          {opportunity.title} · {opportunity.status === "lost" ? "Lost" : "Won"}
        </p>
      }
      onClose={onClose}
      busy={save.isPending}
      size="sm"
    >
      <form onSubmit={submit} noValidate className="space-y-4">
        {banner ? (
          <Alert tone="error" requestId={banner.requestId}>
            {isApiError(save.error, 409) ? "It changed a moment ago. The latest version is loaded now: check and save again." : banner.message}
          </Alert>
        ) : null}
        {general ? <Alert tone="error">{general.join(" ")}</Alert> : null}
        <p className="text-sm text-slate-600">
          The deal&apos;s history keeps the figures before and after, with your reason. Its stage history keeps the value it was{" "}
          {opportunity.status === "lost" ? "lost" : "won"} at.
        </p>
        <TextField
          label={`${CORRECTION_LABELS.value} (₹)`}
          name="value"
          inputMode="decimal"
          value={form.value}
          onChange={(e) => set("value")(e.target.value)}
          errors={problems.value ?? server.value}
          autoComplete="off"
          autoFocus
        />
        <TextField
          label={CORRECTION_LABELS.opportunity_date}
          name="opportunity_date"
          type="date"
          min="2000-01-01"
          max="2099-12-31"
          value={form.opportunity_date}
          onChange={(e) => set("opportunity_date")(e.target.value)}
          errors={problems.opportunity_date ?? server.opportunity_date}
        />
        <TextField
          label={CORRECTION_LABELS.expected_close_date}
          name="expected_close_date"
          type="date"
          min="2000-01-01"
          max="2099-12-31"
          optional
          value={form.expected_close_date}
          onChange={(e) => set("expected_close_date")(e.target.value)}
          errors={server.expected_close_date}
        />
        <TextField
          label="Reason"
          name="reason"
          maxLength={REASON_MAX_LENGTH}
          value={form.reason}
          onChange={(e) => set("reason")(e.target.value)}
          errors={problems.reason ?? server.reason}
          hint="For example the credit note or the approval it follows."
          autoComplete="off"
        />
        <DialogActions>
          <Button variant="secondary" onClick={onClose} disabled={save.isPending}>
            Cancel
          </Button>
          <Button type="submit" loading={save.isPending}>
            Save correction
          </Button>
        </DialogActions>
      </form>
    </Dialog>
  );
}

/** Every correction of the deal's figures, newest first (append-only). Hidden when there is none. */
export function CorrectionHistory({ workspace, opportunityId, version }: { workspace: Workspace; opportunityId: string; version: number }) {
  const titleId = useId();
  const history = useQuery({
    queryKey: [...pipelineKeys.corrections(workspace, opportunityId), version],
    queryFn: () => pipelineApi.corrections(workspace, opportunityId),
    placeholderData: (previous) => previous,
  });
  const rows = history.data?.results;
  if (rows && rows.length === 0) return null;
  return (
    <section aria-labelledby={titleId} className="rounded-lg border border-slate-200 bg-white p-4 sm:p-5">
      <h2 id={titleId} className="mb-3 text-sm font-semibold text-slate-900">
        Corrections
      </h2>
      {history.isError ? (
        <p className="text-sm text-red-700">The corrections couldn&apos;t be loaded. {describeError(history.error).message}</p>
      ) : !rows ? (
        <Skeleton className="h-12 w-full" />
      ) : (
        <ol className="space-y-3">
          {rows.map((row) => (
            <CorrectionRow key={row.id} row={row} />
          ))}
        </ol>
      )}
    </section>
  );
}

function CorrectionRow({ row }: { row: OpportunityCorrection }) {
  return (
    <li className="border-l-2 border-rose-300 pl-3 text-sm">
      <ul className="text-slate-900">
        {row.changes.map((change) => (
          <li key={change.field}>
            {CORRECTION_LABELS[change.field]}: {figure(change.field, change.before)} → <strong className="font-medium">{figure(change.field, change.after)}</strong>
          </li>
        ))}
      </ul>
      {row.reason ? <p className="mt-0.5 break-words text-xs text-slate-600">Reason: {row.reason}</p> : null}
      <p className="text-xs text-slate-500">
        <PersonName person={row.actor} /> · <time dateTime={row.occurred_at}>{formatDateTime(row.occurred_at)}</time>
      </p>
    </li>
  );
}
