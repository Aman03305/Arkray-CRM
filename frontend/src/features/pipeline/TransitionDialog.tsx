"use client";

import { type FormEvent, useId, useState } from "react";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { TextAreaField } from "@/components/ui/Field";
import { Dialog, DialogActions } from "@/components/ui/Dialog";
import type { Stage, StageCategory } from "@/lib/api/types";
import { formatPercent } from "@/lib/money";

import { type AgreedTerms, AgreedTermsFields, parseAgreedTerms } from "./AgreedTerms";
import { StageName } from "./PipelineBits";
import { moveTargets, transitionKind } from "./transitions";
import type { MoveProblem } from "./useMoveOpportunity";

const LOST_REASON_MAX = 500;

export interface TransitionSubject {
  title: string;
  stageId: string;
  status: StageCategory;
}

interface TransitionDialogProps {
  subject: TransitionSubject;
  /** Every stage of the pipeline (targets are derived: active, allowed from here). */
  stages: readonly Stage[];
  /** A chosen target (drag and drop, a menu item), or null to let the user choose. */
  target: Stage | null;
  busy: boolean;
  error: MoveProblem | null;
  /** `terms`: the agreed price and CPT, given when the target is a negotiation stage. */
  onConfirm: (stage: Stage, lostReason: string, terms?: AgreedTerms) => void;
  onClose: () => void;
}

function consequence(kind: ReturnType<typeof transitionKind>, target: Stage): string {
  if (target.type === "negotiation" && (kind === "move" || kind === "reopen")) return "";
  switch (kind) {
    case "win":
      return "It will be closed as won at 100% and leave the open pipeline totals. You can reopen it later.";
    case "lose":
      return "It will be closed as lost at 0% and leave the open pipeline totals. You can reopen it later.";
    case "reopen":
      return `It will be open again at ${formatPercent(target.probability)} (the stage's default) and count towards the pipeline totals.`;
    case "reopen-first":
      return "Won and lost opportunities are reopened into an open stage first.";
    default:
      return `Its probability becomes ${formatPercent(target.probability)}, the stage's default.`;
  }
}

/**
 * Confirms a stage transition, or lets the user pick one (keyboard-friendly alternative
 * to drag and drop). Closing (won/lost) and reopening always ask first; a plain move
 * between open stages only asks when chosen here. Every path calls the same move API.
 */
export function TransitionDialog({ subject, stages, target, busy, error, onConfirm, onClose }: TransitionDialogProps) {
  const choices = moveTargets(stages, subject);
  const [chosen, setChosen] = useState<string>(target?.id ?? "");
  const [reason, setReason] = useState("");
  const [price, setPrice] = useState("");
  const [cpt, setCpt] = useState("");
  const [termsErrors, setTermsErrors] = useState<{ price?: string[]; cpt?: string[] }>({});
  const groupId = useId();
  const stage = target ?? choices.find((s) => s.id === chosen) ?? null;
  const kind = stage ? transitionKind(subject, stage) : null;
  // Entering a negotiation stage (its type, whatever its name) needs the agreed price and the
  // agreed CPT, every time, also after leaving negotiation and coming back (ADR-0029).
  // Cancelling changes nothing.
  const asksTerms = stage?.type === "negotiation" && (kind === "move" || kind === "reopen");

  const title =
    target === null
      ? subject.status === "open"
        ? "Move to stage"
        : "Reopen opportunity"
      : kind === "win"
        ? "Mark as won?"
        : kind === "lose"
          ? "Mark as lost?"
          : kind === "reopen"
            ? `Reopen in ${target.name}?`
            : `Move to ${target.name}?`;

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (busy || !stage || kind === "same" || kind === "reopen-first") return;
    if (asksTerms) {
      const parsed = parseAgreedTerms(price, cpt);
      if (!parsed.ok) {
        setTermsErrors(parsed.errors);
        return;
      }
      setTermsErrors({});
      onConfirm(stage, "", parsed.value);
      return;
    }
    onConfirm(stage, kind === "lose" ? reason.trim() : "");
  };

  return (
    <Dialog open title={title} description={<p>{subject.title}</p>} onClose={onClose} busy={busy} size="sm" role="alertdialog">
      <form onSubmit={submit} className="space-y-4">
        {error ? (
          <Alert tone="error" requestId={error.requestId}>
            {error.message}
          </Alert>
        ) : null}
        {target === null ? (
          <fieldset>
            <legend id={groupId} className="mb-2 text-sm font-medium text-slate-700">
              {subject.status === "open" ? "Move to" : "Reopen in"}
            </legend>
            {choices.length === 0 ? <p className="text-sm text-slate-500">There is no other stage to move to.</p> : null}
            <div className="space-y-1">
              {choices.map((option, index) => (
                <label key={option.id} className="flex cursor-pointer items-center gap-3 rounded-md px-2 py-1.5 hover:bg-slate-50">
                  <input
                    type="radio"
                    name="stage"
                    value={option.id}
                    checked={chosen === option.id}
                    onChange={() => setChosen(option.id)}
                    data-autofocus={index === 0 || undefined}
                    className="size-4 accent-brand-600"
                  />
                  <span className="text-sm text-slate-800">
                    <StageName stage={option} />
                    <span className="ml-1 text-xs text-slate-500">{formatPercent(option.probability)}</span>
                  </span>
                </label>
              ))}
            </div>
          </fieldset>
        ) : null}
        {stage && kind && consequence(kind, stage) ? <p className="text-sm text-slate-600">{consequence(kind, stage)}</p> : null}
        {asksTerms ? (
          <AgreedTermsFields
            price={price}
            cpt={cpt}
            // A field's own error, else the server's refusal of it; typing clears both.
            onPriceChange={(value) => {
              setPrice(value);
              setTermsErrors((e) => ({ ...e, price: [] }));
            }}
            onCptChange={(value) => {
              setCpt(value);
              setTermsErrors((e) => ({ ...e, cpt: [] }));
            }}
            priceErrors={termsErrors.price ?? error?.fields?.negotiated_price}
            cptErrors={termsErrors.cpt ?? error?.fields?.agreed_cpt}
            autoFocus={target !== null}
          />
        ) : null}
        {kind === "lose" ? (
          <TextAreaField
            label="Why was it lost?"
            optional
            rows={3}
            maxLength={LOST_REASON_MAX}
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            hint="For example: chose a competitor, budget cut, no decision. Up to 500 characters."
            data-autofocus={target !== null || undefined}
          />
        ) : null}
        <DialogActions>
          <Button
            variant="secondary"
            onClick={onClose}
            disabled={busy}
            data-autofocus={(target !== null && kind !== "lose" && !asksTerms) || undefined}
          >
            Cancel
          </Button>
          <Button
            type="submit"
            loading={busy}
            disabled={!stage || kind === "reopen-first" || kind === "same"}
            variant={kind === "lose" ? "danger" : "primary"}
          >
            {kind === "win" ? "Mark as won" : kind === "lose" ? "Mark as lost" : kind === "reopen" ? "Reopen" : "Move"}
          </Button>
        </DialogActions>
      </form>
    </Dialog>
  );
}
