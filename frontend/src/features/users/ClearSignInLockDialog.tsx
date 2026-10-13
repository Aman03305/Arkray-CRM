"use client";

import { useMutation } from "@tanstack/react-query";

import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { describeError } from "@/lib/api/errors";
import type { AdminUser } from "@/lib/api/types";
import { formatDateTime } from "@/lib/format";

import { usersApi } from "./api";

/**
 * Lifting the lock too many failed sign-ins in a day put on a user's sign-in from new browsers
 * (review of 73ff1e3, L1), when they can't use the link emailed to them. Audited by the server.
 */
export function ClearSignInLockDialog({ user, onClose, onDone }: {
  user: AdminUser;
  onClose: () => void;
  onDone: (user: AdminUser, message: string) => void;
}) {
  const clear = useMutation({
    mutationFn: () => usersApi.clearSignInLock(user.id),
    onSuccess: (updated) => onDone(updated, `${updated.full_name} can sign in from a new browser again.`),
  });
  return (
    <ConfirmDialog
      open
      title={`Clear the sign-in lock for ${user.full_name}?`}
      confirmLabel="Clear sign-in lock"
      tone="primary"
      busy={clear.isPending}
      error={clear.isError ? describeError(clear.error) : null}
      onConfirm={() => clear.mutate()}
      onCancel={onClose}
    >
      <p>
        Too many failed sign-ins
        {user.sign_in_locked_at ? ` (locked ${formatDateTime(user.sign_in_locked_at)})` : ""} stopped
        this account signing in from browsers it hasn&apos;t been used in. Browsers it has used still
        work.
      </p>
      <p>
        Only clear it once you&apos;ve confirmed who is asking: if someone else was guessing the
        password, they can try again. A new password (Forgot password) is safer.
      </p>
    </ConfirmDialog>
  );
}
