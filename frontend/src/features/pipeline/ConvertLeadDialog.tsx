"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useRef, useState } from "react";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Dialog, DialogActions } from "@/components/ui/Dialog";
import { SelectField, TextField } from "@/components/ui/Field";
import { useFocusFirstInvalid } from "@/components/ui/useFocusFirstInvalid";
import { leadKeys } from "@/features/leads/api";
import { describeError, fieldErrors, isApiError } from "@/lib/api/errors";
import type { Conversion, Lead, LeadConvertRequest } from "@/lib/api/types";
import { formatPercent, parseAmountInput } from "@/lib/money";
import { randomUuid } from "@/lib/random";
import type { Workspace } from "@/lib/workspace";

import { pipelineApi } from "./api";
import { CustomFieldInputs } from "./CustomFields";
import { type CustomValue, customRequest, validateCustom } from "./draft";
import { activeStages, defaultPipeline, firstOpenStage, isNegotiation, syncAfterOpportunityWrite, usePipelines } from "./hooks";

/**
 * Convert a lead: create its opportunity and mark the lead Converted, in one server
 * transaction (all or nothing). Nothing else is created: Arkray CRM has no Company,
 * Account or Contact records. A double click can't convert twice: the request carries the
 * lead's version and an idempotency key reused for an identical retry.
 */
export function ConvertLeadDialog({
  workspace,
  lead,
  onClose,
  onConverted,
}: {
  workspace: Workspace;
  lead: Lead;
  onClose: () => void;
  onConverted: (result: Conversion) => void;
}) {
  const queryClient = useQueryClient();
  const pipelines = usePipelines(workspace);
  const pipeline = defaultPipeline(pipelines.data?.results);
  const [title, setTitle] = useState(() => lead.organization_name || lead.display_name);
  const [value, setValue] = useState("");
  const [stageId, setStageId] = useState("");
  const [closeDate, setCloseDate] = useState("");
  const [negotiatedPrice, setNegotiatedPrice] = useState("");
  const [custom, setCustom] = useState<Record<string, CustomValue>>({});
  const [clientErrors, setClientErrors] = useState<Record<string, string[]>>({});
  const form = useRef<HTMLFormElement>(null);
  const idempotency = useRef<{ body: string; key: string } | null>(null);
  const stages = activeStages(pipeline).filter((s) => s.category !== "lost");
  const stage = stages.find((s) => s.id === stageId) ?? firstOpenStage(pipeline);
  // What the server requires of a new deal (enhancement review): the price when it starts in
  // a negotiation stage (whatever the stage is called), and the pipeline's required fields.
  const asksPrice = isNegotiation(stage);
  const requiredFields = (pipeline?.custom_fields ?? []).filter((f) => f.required);

  const convert = useMutation({
    mutationFn: (body: LeadConvertRequest) => {
      const serialised = JSON.stringify(body);
      if (idempotency.current?.body !== serialised) idempotency.current = { body: serialised, key: randomUuid() };
      return pipelineApi.convert(workspace, lead.id, body, idempotency.current.key);
    },
    onSuccess: (result) => {
      queryClient.setQueryData(leadKeys.detail(workspace, lead.id), result.lead);
      void queryClient.invalidateQueries({ queryKey: leadKeys.all });
      syncAfterOpportunityWrite(queryClient, workspace, result.opportunity);
    },
    onError: (error) => {
      if (isApiError(error, 409)) void queryClient.invalidateQueries({ queryKey: leadKeys.detail(workspace, lead.id) });
    },
  });
  const server = fieldErrors(convert.error);
  const errors = { ...server, ...clientErrors };
  useFocusFirstInvalid(form, Object.keys(clientErrors).length ? clientErrors : convert.error);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (convert.isPending) return;
    const problems: Record<string, string[]> = {};
    if (!title.trim()) problems.title = ["Enter a title."];
    const amount = parseAmountInput(value);
    if (!amount.ok) problems.value = [amount.error];
    const price = parseAmountInput(negotiatedPrice);
    if (asksPrice && !price.ok) problems.negotiated_price = [negotiatedPrice.trim() ? price.error : "Enter the negotiated price."];
    Object.assign(problems, validateCustom(requiredFields, custom, true));
    setClientErrors(problems);
    if (!amount.ok || Object.keys(problems).length) return;
    const body: { -readonly [K in keyof LeadConvertRequest]: LeadConvertRequest[K] } = {
      version: lead.version,
      title: title.trim(),
      value: amount.value,
    };
    if (stageId) body.stage = stageId;
    if (closeDate) body.expected_close_date = closeDate;
    if (asksPrice && price.ok) body.negotiated_price = price.value;
    if (requiredFields.length) body.custom_fields = customRequest(requiredFields, custom);
    convert.mutate(body, { onSuccess: onConverted });
  };

  const problem = convert.isError
    ? isApiError(convert.error, 409)
      ? { message: "This lead was changed a moment ago. The latest details are shown now; please check and try again.", requestId: null }
      : Object.keys(server).some(
            (f) => ["title", "value", "stage", "expected_close_date", "negotiated_price"].includes(f) || f.startsWith("custom_fields."),
          )
        ? null
        : describeError(convert.error)
    : null;

  return (
    <Dialog
      open
      title={`Convert ${lead.display_name}`}
      description={
        <p>
          Creates this lead&apos;s opportunity and marks the lead as Converted, in one step. No company, account or contact records are
          created.
        </p>
      }
      onClose={onClose}
      busy={convert.isPending}
    >
      <form ref={form} onSubmit={submit} noValidate className="space-y-4">
        {problem ? (
          <Alert tone="error" requestId={problem.requestId}>
            {problem.message}
          </Alert>
        ) : null}
        <TextField label="Opportunity title" name="title" value={title} maxLength={200} onChange={(e) => setTitle(e.target.value)} errors={errors.title} data-autofocus />
        <TextField
          label="Value (₹)"
          name="value"
          inputMode="decimal"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          errors={errors.value}
          hint="In rupees, e.g. 12,50,000."
          autoComplete="off"
        />
        <SelectField
          label="Stage"
          name="stage"
          value={stage?.id ?? ""}
          onChange={(e) => setStageId(e.target.value)}
          options={stages.map((s) => ({ value: s.id, label: `${s.name} (${formatPercent(s.probability)})` }))}
          errors={errors.stage}
        />
        {asksPrice ? (
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
        {requiredFields.length ? (
          <CustomFieldInputs fields={requiredFields} values={custom} onChange={(id, v) => setCustom((c) => ({ ...c, [id]: v }))} errors={errors} />
        ) : null}
        <TextField
          label="Expected close date"
          name="expected_close_date"
          type="date"
          min="2000-01-01"
          max="2099-12-31"
          optional
          value={closeDate}
          onChange={(e) => setCloseDate(e.target.value)}
          errors={errors.expected_close_date}
        />
        <DialogActions>
          <Button variant="secondary" onClick={onClose} disabled={convert.isPending}>
            Cancel
          </Button>
          <Button type="submit" loading={convert.isPending}>
            Convert lead
          </Button>
        </DialogActions>
      </form>
    </Dialog>
  );
}
