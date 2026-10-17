"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useEffect, useRef, useState } from "react";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Dialog, DialogActions } from "@/components/ui/Dialog";
import { TextField } from "@/components/ui/Field";
import { Spinner } from "@/components/ui/Spinner";
import { useFocusFirstInvalid } from "@/components/ui/useFocusFirstInvalid";
import { confirmsEmail } from "@/features/privacy/PseudonymiseDialog";
import { type DisplayError, fieldErrors, isApiError } from "@/lib/api/errors";
import type { AdminUser, DeleteAccountRequest, DeletionPreview, Role } from "@/lib/api/types";

import { describeAccountError } from "./account-operations";
import { AccountSummary } from "./AccountSummary";
import { deletionPreviewKey, USERS_QUERY_KEY, usersApi } from "./api";
import { OwnerSelect } from "./OwnerSelect";
import { useOutOfReachOn404 } from "./reach";

const COUNT = new Intl.NumberFormat("en-IN");
const counted = (n: number, one: string, many: string) => `${COUNT.format(n)} ${n === 1 ? one : many}`;

/** "a", "a and b", "a, b and c". */
function joinAnd(parts: readonly string[]): string {
  if (parts.length <= 1) return parts[0] ?? "";
  return `${parts.slice(0, -1).join(", ")} and ${parts.at(-1)}`;
}

const STALE =
  "Someone else changed this account meanwhile. The list has been refreshed: close this dialog and try again.";
const STALLED =
  "The server didn't hand over any more leads, so this stopped. The account is still active. Try again in a moment.";

/**
 * A round that hands over nothing happens now and then (a lead arrived between the server's check
 * and its final step, or one was being edited at that moment) and the same request goes again.
 * This many in a row will not end: stop and say so.
 */
const MAX_IDLE_ROUNDS = 3;

/** The server keeps saying "more to hand over" without handing anything over: repeating would never end. */
class HandoverStalled extends Error {
  constructor() {
    super("No leads were handed over.");
    this.name = "HandoverStalled";
  }
}

/** How one attempt ended without an error: finished, or stopped between two batches. */
interface Outcome {
  /** The administrator stopped it: the account is still active, the leads already moved stay moved. */
  stopped: boolean;
  /** Leads handed over by every request of this attempt. */
  handed: number;
  /** Leads still theirs (0 once finished). */
  remaining: number;
}

function bannerFor(error: unknown): DisplayError {
  if (isApiError(error, 409, "conflict")) return { message: STALE, requestId: null };
  if (error instanceof HandoverStalled) return { message: STALLED, requestId: null };
  return describeAccountError(error, "requests");
}

/** What deleting the account does, in plain language, from the server's counts. */
function Consequences({ preview, role }: { preview: DeletionPreview; role: Role }) {
  const owned = [
    preview.leads > 0 ? counted(preview.leads, "lead", "leads") : null,
    preview.open_deals > 0 ? counted(preview.open_deals, "open deal", "open deals") : null,
    preview.open_activities > 0 ? counted(preview.open_activities, "open task or meeting", "open tasks and meetings") : null,
  ].filter((part): part is string => part !== null);
  const pipelines = preview.personal_pipelines;
  const team = preview.team_members;
  const items = [
    ...(owned.length > 0 ? [`hand over their ${joinAnd(owned)} to the person you choose below`] : []),
    ...(pipelines > 0
      ? [
          pipelines === 1
            ? "archive their personal pipeline if it holds no open deals (it is never deleted)"
            : `archive those of their ${COUNT.format(pipelines)} personal pipelines that hold no open deals (none is deleted)`,
        ]
      : []),
    ...(team > 0
      ? [
          team === 1
            ? "release their team of 1 user, who has no Admin until a Super admin assigns one"
            : `release their team of ${COUNT.format(team)} users, who have no Admin until a Super admin assigns one`,
        ]
      : []),
    ...(role === "team_admin" ? ["cancel the invitations they sent and end the temporary passwords they set"] : []),
    "deactivate the account, sign them out everywhere and erase their name, email and other personal details",
    'keep CRM records and the audit trail, which show them as a "Former user"',
  ];
  return (
    <div className="space-y-2 text-sm text-slate-700">
      {owned.length === 0 ? <p>They don&apos;t own any leads, deals, tasks or meetings, so nothing needs handing over.</p> : null}
      <p>Deleting this account will:</p>
      <ul className="list-disc space-y-1 pl-5">
        {items.map((item) => (
          <li key={item}>{item}</li>
        ))}
      </ul>
    </div>
  );
}

/**
 * Deleting an account (ADR-0036): its work is handed to someone else, it is deactivated and its
 * identity is erased. CRM records and the audit trail are kept; the account stays in the list as
 * a "Former user" (status Deleted). Irreversible, so the administrator types the account's email.
 *
 * An account that owns many leads is handed over in batches: the server answers 202 after each
 * one, and the SAME request is sent again until it answers 200. The dialog shows how far it is
 * and can be stopped between two batches (the account then stays active; what was handed over
 * stays handed over). Leaving the page stops it the same way.
 */
export function DeleteAccountDialog({ user, onClose, onDeleted }: {
  user: AdminUser;
  onClose: () => void;
  /** The account is deleted; `handed` leads (with the work on them) went to someone else. */
  onDeleted: (user: AdminUser, message: string, handed: number) => void;
}) {
  const queryClient = useQueryClient();
  const [typed, setTyped] = useState("");
  const [owner, setOwner] = useState({ id: "", label: "" });
  // The server wanted a new owner although the preview said there was no work: ask for one.
  const [ownerAsked, setOwnerAsked] = useState(false);
  const [progress, setProgress] = useState<{ handed: number; remaining: number } | null>(null);
  const [stopping, setStopping] = useState(false);
  const stop = useRef(false);
  // Leads handed over by the attempt in flight (or the last one): whether what the preview counted is stale.
  const handedOver = useRef(0);
  const form = useRef<HTMLFormElement>(null);

  // Read afresh whenever the dialog opens: the counts change as people work.
  const preview = useQuery({
    queryKey: deletionPreviewKey(user.id),
    queryFn: () => usersApi.deletionPreview(user.id),
    staleTime: 0,
    gcTime: 0,
  });

  const run = useMutation({
    mutationFn: async (body: DeleteAccountRequest): Promise<Outcome> => {
      stop.current = false;
      handedOver.current = 0;
      setStopping(false);
      setProgress(null);
      let handed = 0;
      let idle = 0;
      for (;;) {
        const result = await usersApi.deleteAccount(user.id, body);
        const moved = Number.isFinite(result.reassigned_leads) ? result.reassigned_leads : 0;
        handed += moved;
        handedOver.current = handed;
        if (result.completed) return { stopped: false, handed, remaining: 0 };
        // More to hand over, yet nothing was (or no count came back): a few of these are normal,
        // an endless run of them is not.
        idle = moved === 0 ? idle + 1 : 0;
        if (idle >= MAX_IDLE_ROUNDS) throw new HandoverStalled();
        setProgress({ handed, remaining: result.remaining_leads });
        if (stop.current) return { stopped: true, handed, remaining: result.remaining_leads };
      }
    },
    onSuccess: (outcome) => {
      if (!outcome.stopped) onDeleted(user, `${user.full_name} was deleted.`, outcome.handed);
    },
    onError: (error) => {
      // Out of date (someone changed the account, it can't be deleted any more) or an owner is
      // needed after all: read the account and what it owns again.
      if (isApiError(error, 400) || isApiError(error, 409) || isApiError(error, 422)) {
        void queryClient.invalidateQueries({ queryKey: USERS_QUERY_KEY });
      }
      if (fieldErrors(error).reassign_to) setOwnerAsked(true);
    },
    onSettled: (outcome, error) => {
      // Stopped or failed part-way: what the account still owns is less than the preview counted.
      if ((error || outcome?.stopped) && handedOver.current > 0) {
        void queryClient.invalidateQueries({ queryKey: deletionPreviewKey(user.id) });
      }
    },
  });

  // Leaving the page ends the hand-over after the batch in flight.
  useEffect(() => {
    return () => {
      stop.current = true;
    };
  }, []);

  // Out of reach now (404): the Users page closes this and says why (ADR-0035).
  useOutOfReachOn404(preview.error, user);
  useOutOfReachOn404(run.error, user);
  useFocusFirstInvalid(form, run.error);

  const data = preview.data;
  const blocked = data?.blocked_reason ?? null;
  const asksOwner = (data?.needs_new_owner ?? false) || ownerAsked;
  const canSubmit = data !== undefined && blocked === null && confirmsEmail(typed, user.email) && (!asksOwner || owner.id !== "");
  const running = run.isPending;
  const stoppedEarly = !running && run.data?.stopped === true;

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    if (running || !canSubmit) return;
    // The same request every time: the server tells the batches apart, not the client.
    run.mutate({
      version: user.version,
      confirm_email: typed.trim(),
      ...(asksOwner && owner.id ? { reassign_to: owner.id } : {}),
    });
  };

  const server = fieldErrors(run.error);
  const handedBefore = run.isError && progress && progress.handed > 0 ? progress.handed : 0;
  const refusal = run.isError && !server.reassign_to?.length ? bannerFor(run.error) : null;
  // Not said twice: a refusal the account's own "can't be deleted" explanation already gives.
  const banner = refusal && !(refusal.message === blocked && handedBefore === 0) ? refusal : null;
  const previewProblem = preview.isError && data === undefined ? describeAccountError(preview.error, "requests") : null;

  return (
    <Dialog
      open
      role="alertdialog"
      title={`Delete ${user.full_name}?`}
      description={
        <p>
          This can&apos;t be undone. Their work is handed to someone else, the account is deactivated and their personal
          details are erased. CRM records and the audit trail are kept.
        </p>
      }
      onClose={onClose}
      busy={running}
    >
      <form ref={form} onSubmit={onSubmit} noValidate className="space-y-4 [overflow-wrap:anywhere]">
        <AccountSummary user={user} />
        {banner ? (
          <Alert tone="error" requestId={banner.requestId}>
            {banner.message}
            {handedBefore > 0 ? (
              <span className="mt-1 block">{`${counted(handedBefore, "lead was", "leads were")} already handed over; the account is still active.`}</span>
            ) : null}
          </Alert>
        ) : null}
        {data === undefined ? (
          previewProblem ? (
            <Alert
              tone="error"
              requestId={previewProblem.requestId}
              action={
                <Button variant="secondary" size="sm" onClick={() => void preview.refetch()} loading={preview.isFetching}>
                  Try again
                </Button>
              }
            >
              {`We couldn't check what deleting this account would do. ${previewProblem.message}`}
            </Alert>
          ) : (
            <div role="status" aria-busy="true" className="flex items-center gap-2 text-sm text-slate-600">
              <Spinner />
              Checking what this account owns…
            </div>
          )
        ) : blocked ? (
          <Alert tone="error" title="This account can't be deleted">
            {blocked}
          </Alert>
        ) : (
          <>
            <Consequences preview={data} role={user.role} />
            {asksOwner ? (
              <OwnerSelect
                label="Hand their work to"
                name="reassign_to"
                value={owner.id}
                valueLabel={owner.label}
                onChange={(id, label) => setOwner({ id, label })}
                placeholder="Choose who takes over"
                excludeId={user.id}
                errors={server.reassign_to}
                hint="They become the owner of those leads and of the open deals, tasks and meetings on them."
              />
            ) : null}
            <TextField
              label="Type their email to confirm"
              name="confirm_email"
              type="email"
              inputMode="email"
              autoComplete="off"
              spellCheck={false}
              maxLength={254}
              value={typed}
              onChange={(e) => setTyped(e.target.value)}
              readOnly={running}
              // What to type; a long address wraps instead of widening the dialog on a phone.
              hint={<span className="[overflow-wrap:anywhere]">{user.email}</span>}
            />
          </>
        )}
        {running && progress ? (
          <div
            role="status"
            className="flex items-start gap-2 rounded-md border border-slate-200 bg-slate-50 px-3.5 py-3 text-sm text-slate-700"
          >
            <Spinner className="mt-0.5 size-4 shrink-0" />
            <p>{`Handed over ${counted(progress.handed, "lead", "leads")}, ${COUNT.format(progress.remaining)} to go…${stopping ? " Stopping after this batch." : ""}`}</p>
          </div>
        ) : null}
        {stoppedEarly && run.data ? (
          <Alert tone="info" title="Stopped before finishing">
            {`${counted(run.data.handed, "lead was", "leads were")} handed over, ${COUNT.format(run.data.remaining)} to go. The account hasn't been deleted: delete it again to carry on.`}
          </Alert>
        ) : null}
        <DialogActions>
          {/* While it runs, Stop takes Cancel's place: the same button, so focus stays where it was. */}
          {running ? (
            <Button
              variant="secondary"
              loading={stopping}
              onClick={() => {
                stop.current = true;
                setStopping(true);
              }}
            >
              {stopping ? "Stopping…" : "Stop"}
            </Button>
          ) : (
            <Button variant="secondary" onClick={onClose} data-autofocus>
              {stoppedEarly ? "Close" : "Cancel"}
            </Button>
          )}
          <Button type="submit" variant="danger" disabled={!canSubmit} loading={running}>
            Delete account
          </Button>
        </DialogActions>
      </form>
    </Dialog>
  );
}
