"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { type FormEvent, useRef, useState } from "react";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Dialog, DialogActions } from "@/components/ui/Dialog";
import { TextAreaField } from "@/components/ui/Field";
import { describeError, fieldErrors } from "@/lib/api/errors";
import type { AdminUser } from "@/lib/api/types";
import { VIEWER_QUERY_KEY } from "@/lib/query-client";
import type { Viewer } from "@/lib/viewer";
import { userWorkspaceHref } from "@/lib/workspace";

import { SECURITY_EVENTS_QUERY_KEY, supportApi, toSupportSession } from "./api";

const REASON_MAX = 200;

/**
 * Starts a support session: the administrator works in this one user's CRM, still signed in
 * as themselves (never with the user's password), for a limited time, recorded in the
 * security log. The shell then shows the session banner and keeps them in that workspace.
 */
export function AccessAsUserDialog({ user, onClose }: { user: AdminUser; onClose: () => void }) {
  const [reason, setReason] = useState("");
  const router = useRouter();
  const queryClient = useQueryClient();
  const starting = useRef(false);
  const start = useMutation({
    mutationFn: () => supportApi.start({ user: user.id, reason: reason.trim() }),
    onSuccess: (session) => {
      // The shell follows the viewer: carry the session now, then confirm it from the API.
      queryClient.setQueryData<Viewer>(VIEWER_QUERY_KEY, (viewer) =>
        viewer ? { ...viewer, supportSession: toSupportSession(session) } : viewer,
      );
      void queryClient.invalidateQueries({ queryKey: VIEWER_QUERY_KEY });
      void queryClient.invalidateQueries({ queryKey: SECURITY_EVENTS_QUERY_KEY });
      router.push(userWorkspaceHref(session.target.id));
    },
  });

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    // A ref, not the mutation state: two clicks inside one frame both see "not pending".
    if (starting.current || start.isSuccess) return;
    starting.current = true;
    start.mutate(undefined, { onSettled: () => (starting.current = false) });
  };

  const reasonErrors = fieldErrors(start.error).reason;
  const banner = start.isError && !reasonErrors ? describeError(start.error) : null;

  return (
    <Dialog
      open
      title={`Access ${user.full_name}'s CRM?`}
      description="You stay signed in as yourself, for a limited time. This is recorded."
      onClose={onClose}
      busy={start.isPending}
      size="sm"
    >
      <form onSubmit={onSubmit} noValidate className="space-y-4">
        {banner ? (
          <Alert tone="error" requestId={banner.requestId}>
            {banner.message}
          </Alert>
        ) : null}
        <TextAreaField
          label="Reason"
          name="reason"
          optional
          rows={2}
          maxLength={REASON_MAX}
          data-autofocus
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          errors={reasonErrors}
          hint={`${reason.length}/${REASON_MAX}`}
        />
        <DialogActions>
          <Button variant="secondary" onClick={onClose} disabled={start.isPending}>
            Cancel
          </Button>
          <Button type="submit" loading={start.isPending || start.isSuccess}>
            Start support session
          </Button>
        </DialogActions>
      </form>
    </Dialog>
  );
}
