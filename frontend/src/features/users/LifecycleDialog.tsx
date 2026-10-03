"use client";

import { useMutation } from "@tanstack/react-query";

import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { describeError } from "@/lib/api/errors";
import type { AdminUser } from "@/lib/api/types";

import { usersApi } from "./api";

export type LifecycleAction = "deactivate" | "activate" | "resend";

function copy(action: LifecycleAction, user: AdminUser) {
  switch (action) {
    case "deactivate":
      return {
        title: `Deactivate ${user.full_name}?`,
        confirm: "Deactivate",
        tone: "danger" as const,
        body: [
          "They'll be signed out everywhere immediately and won't be able to sign in.",
          "Their records and history are kept, and you can reactivate them later.",
        ],
        done: (u: AdminUser) => `${u.full_name} was deactivated.`,
      };
    case "activate":
      return {
        title: `Reactivate ${user.full_name}?`,
        confirm: "Reactivate",
        tone: "primary" as const,
        body: [
          user.activated_at
            ? "They'll be able to sign in again with their existing password."
            : "They never set a password, so a new invitation will be emailed to them.",
        ],
        done: (u: AdminUser) =>
          u.status === "invited" ? `${u.full_name} was reactivated and re-invited.` : `${u.full_name} was reactivated.`,
      };
    case "resend":
      return {
        title: "Resend invitation?",
        confirm: "Resend invitation",
        tone: "primary" as const,
        body: [`A new invitation link will be emailed to ${user.email}.`, "The previous link will stop working."],
        done: (u: AdminUser) => `A new invitation is on its way to ${u.email}.`,
      };
  }
}

const RUN: Record<LifecycleAction, (id: string) => Promise<AdminUser>> = {
  deactivate: usersApi.deactivate,
  activate: usersApi.activate,
  resend: usersApi.resendInvitation,
};

export function LifecycleDialog({ action, user, onClose, onDone }: {
  action: LifecycleAction;
  user: AdminUser;
  onClose: () => void;
  onDone: (user: AdminUser, message: string) => void;
}) {
  const text = copy(action, user);
  const run = useMutation({
    mutationFn: () => RUN[action](user.id),
    onSuccess: (updated) => onDone(updated, text.done(updated)),
  });
  return (
    <ConfirmDialog
      open
      title={text.title}
      confirmLabel={text.confirm}
      tone={text.tone}
      busy={run.isPending}
      error={run.isError ? describeError(run.error) : null}
      onConfirm={() => run.mutate()}
      onCancel={onClose}
    >
      {text.body.map((line) => (
        <p key={line}>{line}</p>
      ))}
    </ConfirmDialog>
  );
}
