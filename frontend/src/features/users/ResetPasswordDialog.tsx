"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";

import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { type DisplayError, isApiError, tryAgainIn } from "@/lib/api/errors";
import type { AdminUser } from "@/lib/api/types";

import { describeAccountError } from "./account-operations";
import { AccountSummary } from "./AccountSummary";
import { USERS_QUERY_KEY, usersApi } from "./api";
import { useOutOfReachOn404 } from "./reach";

/** The request throttle's own words (the app never shows them); the account's own limit has others. */
const REQUEST_THROTTLE = /^Request was throttled\b/i;

/**
 * Why a reset link wasn't sent, in words for the dialog. A 429 is usually the account's own
 * limit (3 links an hour, so nobody floods the owner's mailbox, ADR-0036), which says what to do
 * instead; a refusal of the account (403: a Super admin's) keeps the server's explanation, and
 * the rest (422: not active or already deleted) are the server's messages.
 */
export function describeResetError(error: unknown): DisplayError {
  if (isApiError(error, 429) && !REQUEST_THROTTLE.test(error.message)) {
    return {
      message: `A reset link was already sent to this account several times in the last hour. Ask them to use the latest one, or try again ${tryAgainIn(error.retryAfterSeconds)}.`,
      requestId: error.requestId,
    };
  }
  return describeAccountError(error, "reset links");
}

/**
 * Having a password reset link emailed to an account (ADR-0036), for someone who forgot theirs.
 * The administrator never sees or chooses a password: the account's current one stops working,
 * its sessions end, and a single-use link goes to the account's own address, where its owner
 * chooses the next. Consequential (they are signed out at once), so an alertdialog whose focus
 * starts on Cancel.
 */
export function ResetPasswordDialog({ user, onClose, onDone }: {
  user: AdminUser;
  onClose: () => void;
  onDone: (user: AdminUser, message: string) => void;
}) {
  const queryClient = useQueryClient();
  const reset = useMutation({
    mutationFn: () => usersApi.resetPassword(user.id),
    onSuccess: (updated) => onDone(updated, `Reset link sent to ${updated.email}.`),
    onError: (error) => {
      // What the list showed was out of date (no longer active, already deleted): read it again.
      if (isApiError(error, 422)) void queryClient.invalidateQueries({ queryKey: USERS_QUERY_KEY });
    },
  });
  // Out of reach now (404): the Users page closes this and says why (ADR-0035).
  useOutOfReachOn404(reset.error, user);

  return (
    <ConfirmDialog
      open
      title={`Reset password for ${user.full_name}?`}
      confirmLabel="Send reset link"
      tone="danger"
      busy={reset.isPending}
      error={reset.isError ? describeResetError(reset.error) : null}
      onConfirm={() => reset.mutate()}
      onCancel={onClose}
    >
      <AccountSummary user={user} />
      <p>
        We&apos;ll email a single-use link to the address above, where they choose a new password. It expires soon.
      </p>
      <p>
        Their current password stops working right away and they&apos;re signed out everywhere. You never see or choose
        their password. They&apos;re also emailed that an administrator did this.
      </p>
    </ConfirmDialog>
  );
}
