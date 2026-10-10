"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useRef, useState } from "react";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Dialog, DialogActions } from "@/components/ui/Dialog";
import { TextField } from "@/components/ui/Field";
import { useFocusFirstInvalid } from "@/components/ui/useFocusFirstInvalid";
import { syncAfterOpportunityWrite } from "@/features/pipeline/hooks";
import { searchKeys } from "@/features/search/api";
import { describeError, fieldErrors, isApiError } from "@/lib/api/errors";
import type { CustomerCorrectionRequest, CustomerCorrectionResult, Lead } from "@/lib/api/types";
import type { Workspace } from "@/lib/workspace";

import { leadKeys, leadsApi } from "./api";

/** The customer's details a correction may change (pipeline.corrections.CORRECTABLE). */
type Correctable = Exclude<keyof CustomerCorrectionRequest, "version">;
type Draft = Record<Correctable, string>;

interface FieldSpec {
  name: Correctable;
  label: string;
  maxLength: number;
  type?: "email" | "tel";
  hint?: string;
  /** Half a row from the small breakpoint up. */
  half?: boolean;
}

// The lead's own length limits (leads.models); the server checks them again.
const GROUPS: { legend: string; fields: FieldSpec[] }[] = [
  {
    legend: "Customer",
    fields: [
      { name: "first_name", label: "First name", maxLength: 100, half: true },
      { name: "last_name", label: "Last name", maxLength: 100, half: true },
      { name: "organization_name", label: "Organisation", maxLength: 200, half: true },
      { name: "job_title", label: "Job title", maxLength: 100, half: true },
    ],
  },
  {
    legend: "Contact",
    fields: [
      { name: "email", label: "Email", maxLength: 254, type: "email" },
      { name: "phone", label: "Phone", maxLength: 40, type: "tel", half: true },
      { name: "mobile", label: "Mobile", maxLength: 40, type: "tel", half: true },
      { name: "alternate_phone", label: "Alternate phone", maxLength: 40, type: "tel", half: true },
    ],
  },
  {
    legend: "Address",
    fields: [
      { name: "address_line_1", label: "Address line 1", maxLength: 200 },
      { name: "address_line_2", label: "Address line 2", maxLength: 200 },
      { name: "city", label: "City", maxLength: 100, half: true },
      { name: "state", label: "State", maxLength: 100, half: true },
      { name: "postal_code", label: "Postal code", maxLength: 20, half: true },
      { name: "country", label: "Country code", maxLength: 2, half: true, hint: "Two letters, e.g. IN." },
    ],
  },
];
const FIELDS = GROUPS.flatMap((group) => group.fields);

function draftOf(lead: Lead): Draft {
  return Object.fromEntries(FIELDS.map((field) => [field.name, lead[field.name]])) as Draft;
}

/** Only what differs from the details the form started from (blank clears a field). */
function changesOf(base: Lead, draft: Draft): Partial<Draft> {
  const changes: Partial<Draft> = {};
  for (const { name } of FIELDS) {
    const value = draft[name].trim();
    if (value !== base[name]) changes[name] = value;
  }
  return changes;
}

export function correctionMessage(result: CustomerCorrectionResult): string {
  const deals = result.opportunities;
  return deals === 0
    ? "Customer details corrected."
    : `Customer details corrected; ${deals} ${deals === 1 ? "deal" : "deals"} updated.`;
}

/**
 * Correcting a customer's details on their request (docs/privacy.md#correction): a wrong
 * email, a misspelt name, an old address. Only the fields changed here are sent, with the
 * version the form started from, so another person's change in between is a conflict, never
 * overwritten. The server corrects the lead and every deal whose copy still shows the old
 * value; a copy someone deliberately made different, and everything commercial (amounts,
 * prices, stages, notes, history), stays as it is.
 */
export function CorrectDetailsDialog({
  workspace,
  lead,
  onClose,
  onDone,
}: {
  workspace: Workspace;
  lead: Lead;
  onClose: () => void;
  onDone: (message: string) => void;
}) {
  const queryClient = useQueryClient();
  // The details (and version) this form started from: the changes are measured against them.
  const [base, setBase] = useState(lead);
  const [draft, setDraft] = useState<Draft>(() => draftOf(lead));
  const [unchanged, setUnchanged] = useState(false);
  const form = useRef<HTMLFormElement>(null);

  const save = useMutation({
    mutationFn: (body: CustomerCorrectionRequest) => leadsApi.correct(workspace, base.id, body),
    onSuccess: (result) => {
      queryClient.setQueryData(leadKeys.detail(workspace, result.lead.id), result.lead);
      // The deals' copies (names, contacts), their timelines and activities, and search.
      syncAfterOpportunityWrite(queryClient, workspace);
      void queryClient.invalidateQueries({ queryKey: searchKeys.all });
      onDone(correctionMessage(result));
    },
    onError: (error) => {
      if (isApiError(error, 409) || isApiError(error, 422)) {
        void queryClient.invalidateQueries({ queryKey: leadKeys.detail(workspace, base.id) });
      }
    },
  });

  // After a conflict: start again from the latest details (nothing typed is sent unseen).
  const reload = useMutation({
    mutationFn: () => leadsApi.get(workspace, base.id),
    onSuccess: (latest) => {
      queryClient.setQueryData(leadKeys.detail(workspace, latest.id), latest);
      setBase(latest);
      setDraft(draftOf(latest));
      save.reset();
    },
  });

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    if (save.isPending) return;
    const changes = changesOf(base, draft);
    if (Object.keys(changes).length === 0) {
      setUnchanged(true);
      return;
    }
    setUnchanged(false);
    save.mutate({ version: base.version, ...changes });
  };

  const errors = fieldErrors(save.error);
  useFocusFirstInvalid(form, save.error);
  const stale = isApiError(save.error, 409);
  const known = FIELDS.some((field) => errors[field.name]?.length);
  const banner = save.isError && !known && !stale ? describeError(save.error) : null;
  const busy = save.isPending || reload.isPending;

  return (
    <Dialog
      open
      title="Correct customer details"
      description="Corrects this customer's details, and every deal still showing the old value. Deal amounts, prices and history don't change."
      onClose={onClose}
      busy={busy}
      size="lg"
    >
      <form ref={form} onSubmit={onSubmit} noValidate className="space-y-5">
        {stale ? (
          <Alert
            tone="error"
            action={
              <Button variant="secondary" size="sm" onClick={() => reload.mutate()} loading={reload.isPending}>
                Reload latest details
              </Button>
            }
          >
            Someone else changed this customer&apos;s details meanwhile. Reload the latest details and make your correction again.
            {reload.isError ? ` ${describeError(reload.error).message}` : ""}
          </Alert>
        ) : banner ? (
          <Alert tone="error" requestId={banner.requestId}>
            {errors.non_field_errors?.join(" ") ?? banner.message}
          </Alert>
        ) : null}
        {unchanged && !save.isError ? <Alert tone="info">Nothing has changed yet: edit the details to correct.</Alert> : null}
        {GROUPS.map((group) => (
          <fieldset key={group.legend} className="space-y-3">
            <legend className="mb-2 text-sm font-semibold text-slate-900">{group.legend}</legend>
            <div className="grid gap-3 sm:grid-cols-2">
              {group.fields.map((field, index) => (
                <TextField
                  key={field.name}
                  className={field.half ? undefined : "sm:col-span-2"}
                  label={field.label}
                  name={field.name}
                  type={field.type ?? "text"}
                  inputMode={field.type}
                  autoComplete="off"
                  maxLength={field.maxLength}
                  data-autofocus={group === GROUPS[0] && index === 0 ? true : undefined}
                  value={draft[field.name]}
                  onChange={(e) => {
                    const value = e.target.value;
                    setDraft((d) => ({ ...d, [field.name]: value }));
                    setUnchanged(false);
                  }}
                  errors={errors[field.name]}
                  hint={field.hint}
                />
              ))}
            </div>
          </fieldset>
        ))}
        <DialogActions>
          <Button variant="secondary" onClick={onClose} disabled={busy}>
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
