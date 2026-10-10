"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { type FormEvent, useEffect, useId, useRef, useState } from "react";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Dialog, DialogActions } from "@/components/ui/Dialog";
import { TextField } from "@/components/ui/Field";
import { useFocusFirstInvalid } from "@/components/ui/useFocusFirstInvalid";
import { describeError, fieldErrors } from "@/lib/api/errors";
import type { DataSubjectType } from "@/lib/api/types";

import { DATA_REQUESTS_HREF, privacyApi, privacyKeys, REFERENCE_MAX, REFERENCE_PATTERN } from "./api";

/**
 * Requesting an export of one person's data (a customer's lead, or a staff member): the
 * request's ticket or case reference, and the administrator's confirmation that they have
 * verified the requester's identity. A job builds the ZIP; it is downloaded from the Data
 * requests page, by the administrator who asked for it only.
 */
export function ExportDataDialog({
  subject,
  onClose,
}: {
  subject: { type: DataSubjectType; id: string; name: string };
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const [reference, setReference] = useState("");
  const [verified, setVerified] = useState(false);
  const [missing, setMissing] = useState<Record<string, string[]>>({});
  const form = useRef<HTMLFormElement>(null);
  const checkId = useId();
  const checkErrorId = useId();

  const request = useMutation({
    mutationFn: () =>
      privacyApi.requestExport({ subject_type: subject.type, subject_id: subject.id, reference: reference.trim(), identity_verified: true }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: privacyKeys.exports }),
  });
  // The form (and its focused button) gives way to the outcome: focus follows to its Close.
  const done = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (request.isSuccess) done.current?.querySelector<HTMLElement>("button")?.focus();
  }, [request.isSuccess]);

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    if (request.isPending || request.isSuccess) return;
    const problems: Record<string, string[]> = {};
    const value = reference.trim();
    if (!value) problems.reference = ["Enter the request's ticket or case reference."];
    else if (!REFERENCE_PATTERN.test(value)) {
      problems.reference = ["Use the ticket or case reference only: letters, digits and . _ / # - (no spaces)."];
    }
    if (!verified) problems.identity_verified = ["Confirm that you have verified the requester's identity."];
    setMissing(problems);
    if (Object.keys(problems).length === 0) request.mutate();
  };

  const errors = { ...fieldErrors(request.error), ...missing };
  useFocusFirstInvalid(form, Object.keys(missing).length ? missing : request.error);
  const known = ["reference", "identity_verified", "subject_id", "subject_type"];
  const banner = request.isError && !known.some((field) => fieldErrors(request.error)[field]?.length) ? describeError(request.error) : null;
  const subjectError = [...(errors.subject_id ?? []), ...(errors.subject_type ?? [])];
  const kind = subject.type === "lead" ? "customer" : "user";

  return (
    <Dialog
      open
      title={`Export ${subject.name}'s data`}
      description={`A ZIP of this ${kind}'s data, for a request they made. Only you can download it, until it expires.`}
      onClose={onClose}
      busy={request.isPending}
      size="sm"
    >
      {request.isSuccess ? (
        <div ref={done} className="space-y-4">
          <Alert tone="success" title="Export requested">
            It&apos;s being prepared. Download it from{" "}
            <Link href={DATA_REQUESTS_HREF} className="font-medium underline">
              Data requests
            </Link>{" "}
            when it&apos;s ready.
          </Alert>
          <DialogActions>
            <Button variant="secondary" onClick={onClose}>
              Close
            </Button>
          </DialogActions>
        </div>
      ) : (
        <form ref={form} onSubmit={onSubmit} noValidate className="space-y-4">
          {banner ? (
            <Alert tone="error" requestId={banner.requestId}>
              {fieldErrors(request.error).non_field_errors?.join(" ") ?? banner.message}
            </Alert>
          ) : null}
          {subjectError.length ? <Alert tone="error">{subjectError.join(" ")}</Alert> : null}
          <TextField
            label="Request reference"
            name="reference"
            autoComplete="off"
            data-autofocus
            maxLength={REFERENCE_MAX}
            value={reference}
            onChange={(e) => setReference(e.target.value)}
            errors={errors.reference}
            hint="The ticket or case number, e.g. DSR-2026-014. Not a description of the person."
          />
          <div>
            <label htmlFor={checkId} className="flex items-start gap-2 text-sm text-slate-800">
              <input
                id={checkId}
                type="checkbox"
                className="mt-0.5 size-4 shrink-0 accent-brand-600"
                checked={verified}
                onChange={(e) => setVerified(e.target.checked)}
                aria-invalid={errors.identity_verified?.length ? true : undefined}
                aria-describedby={errors.identity_verified?.length ? checkErrorId : undefined}
              />
              I have verified the requester&apos;s identity
            </label>
            {errors.identity_verified?.length ? (
              <p id={checkErrorId} className="mt-1.5 pl-6 text-xs text-red-600">
                {errors.identity_verified.join(" ")}
              </p>
            ) : null}
          </div>
          <DialogActions>
            <Button variant="secondary" onClick={onClose} disabled={request.isPending}>
              Cancel
            </Button>
            <Button type="submit" loading={request.isPending}>
              Request export
            </Button>
          </DialogActions>
        </form>
      )}
    </Dialog>
  );
}
