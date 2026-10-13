"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ShieldCheck } from "lucide-react";
import { type FormEvent, useRef, useState } from "react";

import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Dialog, DialogActions } from "@/components/ui/Dialog";
import { PasswordField } from "@/components/ui/Field";
import { Skeleton } from "@/components/ui/Skeleton";
import { useFocusFirstInvalid } from "@/components/ui/useFocusFirstInvalid";
import { MFA_STATUS_QUERY_KEY, mfaApi } from "@/features/auth/api";
import { FactorField, factorProblem, normalizeCode, serverCodeErrors } from "@/features/auth/FactorField";
import { MfaSetupFlow } from "@/features/auth/MfaSetupFlow";
import { RecoveryCodes } from "@/features/auth/RecoveryCodes";
import { describeError, fieldErrors } from "@/lib/api/errors";
import { formatDate } from "@/lib/format";
import { VIEWER_QUERY_KEY } from "@/lib/query-client";

/** Few enough recovery codes left to suggest new ones. */
const LOW_RECOVERY_CODES = 3;

/** Replacing the recovery codes: a code from the app, then the new codes, once. */
function ReplaceCodesDialog({ onClose, onReplaced }: { onClose: () => void; onReplaced: () => void }) {
  const [code, setCode] = useState("");
  const [missing, setMissing] = useState<string | null>(null);
  const [codes, setCodes] = useState<readonly string[] | null>(null);
  const form = useRef<HTMLFormElement>(null);
  const replace = useMutation({
    mutationFn: () => mfaApi.replaceRecoveryCodes(normalizeCode(code)),
    gcTime: 0, // the answer holds the new codes
    onSuccess: (answer) => {
      setCode("");
      setCodes(answer.recovery_codes);
      onReplaced();
    },
  });
  useFocusFirstInvalid(form, missing ?? replace.error);

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    if (replace.isPending) return;
    const problem = factorProblem("code", code);
    setMissing(problem);
    if (!problem) replace.mutate();
  };

  const server = serverCodeErrors(replace.error);
  const errors = missing ? [missing] : server;
  const banner = replace.isError && !server ? describeError(replace.error, { tooMany: "attempts" }) : null;

  return (
    <Dialog
      open
      title="Replace recovery codes"
      description={codes ? undefined : "Your current recovery codes stop working, and you get ten new ones."}
      onClose={onClose}
      busy={replace.isPending}
    >
      {codes ? (
        <RecoveryCodes codes={codes} onDone={onClose} doneLabel="Done" />
      ) : (
        <form ref={form} onSubmit={onSubmit} noValidate className="space-y-4">
          {banner ? (
            <Alert tone="error" requestId={banner.requestId}>
              {banner.message}
            </Alert>
          ) : null}
          <FactorField kind="code" value={code} onChange={setCode} errors={errors} dataAutofocus />
          <DialogActions>
            <Button variant="secondary" onClick={onClose} disabled={replace.isPending}>
              Cancel
            </Button>
            <Button type="submit" loading={replace.isPending}>
              Replace codes
            </Button>
          </DialogActions>
        </form>
      )}
    </Dialog>
  );
}

/** Turning it off: the password and a code (offered only when the account may go without). */
function DisableDialog({ onClose, onDisabled }: { onClose: () => void; onDisabled: () => void }) {
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [missing, setMissing] = useState<Record<string, string>>({});
  const form = useRef<HTMLFormElement>(null);
  const disable = useMutation({
    mutationFn: () => mfaApi.disable(password, normalizeCode(code)),
    gcTime: 0, // the request held the password and the code
    onSuccess: () => {
      setPassword("");
      setCode("");
      onDisabled();
    },
  });
  useFocusFirstInvalid(form, Object.keys(missing).length ? missing : disable.error);

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    if (disable.isPending) return;
    const problems: Record<string, string> = {};
    if (!password) problems.password = "Enter your current password.";
    const codeProblem = factorProblem("code", code);
    if (codeProblem) problems.code = codeProblem;
    setMissing(problems);
    if (Object.keys(problems).length === 0) disable.mutate();
  };

  const serverPassword = fieldErrors(disable.error).current_password;
  const serverCode = serverCodeErrors(disable.error);
  const passwordErrors = missing.password ? [missing.password] : serverPassword;
  const codeErrors = missing.code ? [missing.code] : serverCode;
  const banner = disable.isError && !serverPassword && !serverCode ? describeError(disable.error, { tooMany: "attempts" }) : null;

  return (
    <Dialog
      open
      role="alertdialog"
      title="Turn off two-step verification?"
      description="Signing in will need only your password. Your recovery codes stop working, and you're signed out on your other devices."
      onClose={onClose}
      busy={disable.isPending}
    >
      <form ref={form} onSubmit={onSubmit} noValidate className="space-y-4">
        {banner ? (
          <Alert tone="error" requestId={banner.requestId}>
            {banner.message}
          </Alert>
        ) : null}
        <PasswordField
          label="Current password"
          name="current-password"
          autoComplete="current-password"
          data-autofocus
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          errors={passwordErrors}
        />
        <FactorField kind="code" value={code} onChange={setCode} errors={codeErrors} />
        <DialogActions>
          <Button variant="secondary" onClick={onClose} disabled={disable.isPending}>
            Cancel
          </Button>
          <Button type="submit" variant="danger" loading={disable.isPending}>
            Turn off
          </Button>
        </DialogActions>
      </form>
    </Dialog>
  );
}

type Open = "setup" | "replace" | "disable" | null;

/**
 * The viewer's own two-step verification in Settings (ADR-0033): whether it's on, setting it
 * up, replacing the recovery codes, and turning it off where the account may go without. Not
 * rendered during a support session (the API refuses every two-step change there).
 */
export function TwoStepSection() {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState<Open>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const status = useQuery({ queryKey: MFA_STATUS_QUERY_KEY, queryFn: mfaApi.status });

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: MFA_STATUS_QUERY_KEY });
    void queryClient.invalidateQueries({ queryKey: VIEWER_QUERY_KEY });
  };

  const start = (next: Open) => {
    setNotice(null);
    setOpen(next);
  };

  if (open === "setup") {
    return (
      <MfaSetupFlow
        onCancel={() => setOpen(null)}
        onComplete={() => {
          setOpen(null);
          setNotice("Two-step verification is on. You've been signed out on your other devices.");
          refresh();
        }}
      />
    );
  }

  if (status.isPending) {
    return (
      <div aria-busy="true">
        <Skeleton className="h-4 w-48" />
        <span className="sr-only">Loading</span>
      </div>
    );
  }
  if (status.isError) {
    const error = describeError(status.error);
    return (
      <Alert
        tone="error"
        requestId={error.requestId}
        action={
          <Button variant="secondary" size="sm" onClick={() => void status.refetch()} loading={status.isFetching}>
            Try again
          </Button>
        }
      >
        Two-step verification couldn&apos;t be loaded. {error.message}
      </Alert>
    );
  }

  const mfa = status.data;
  const low = mfa.enabled && mfa.recovery_codes_remaining <= LOW_RECOVERY_CODES;
  return (
    <div className="space-y-4">
      <div aria-live="polite" className="empty:hidden">
        {notice ? <Alert tone="success">{notice}</Alert> : null}
      </div>
      <dl className="grid gap-4 sm:grid-cols-3">
        <div>
          <dt className="text-xs font-medium uppercase tracking-wide text-slate-500">Status</dt>
          <dd className="mt-1 text-sm text-slate-900">
            {mfa.enabled ? <Badge tone="green">On</Badge> : <Badge>Off</Badge>}
            {mfa.enabled && mfa.confirmed_at ? <span className="ml-2 text-slate-500">since {formatDate(mfa.confirmed_at)}</span> : null}
          </dd>
        </div>
        {mfa.enabled ? (
          <div>
            <dt className="text-xs font-medium uppercase tracking-wide text-slate-500">Recovery codes</dt>
            <dd className={`mt-1 text-sm ${low ? "text-red-600" : "text-slate-900"}`}>
              {mfa.recovery_codes_remaining} left{low ? ". Replace them soon." : ""}
            </dd>
          </div>
        ) : null}
        {mfa.required ? (
          <div>
            <dt className="text-xs font-medium uppercase tracking-wide text-slate-500">Policy</dt>
            <dd className="mt-1 text-sm text-slate-900">Required for your account</dd>
          </div>
        ) : null}
      </dl>
      {mfa.enabled ? (
        <div className="flex flex-wrap gap-2">
          <Button variant="secondary" onClick={() => start("replace")}>
            Replace recovery codes
          </Button>
          {mfa.can_disable ? (
            <Button variant="secondary" onClick={() => start("disable")}>
              Turn off
            </Button>
          ) : null}
        </div>
      ) : (
        <Button icon={<ShieldCheck aria-hidden="true" className="size-4" />} onClick={() => start("setup")}>
          Set up two-step verification
        </Button>
      )}
      {open === "replace" ? (
        <ReplaceCodesDialog
          onClose={() => setOpen(null)}
          onReplaced={() => {
            setNotice("New recovery codes are ready. The old ones no longer work.");
            refresh();
          }}
        />
      ) : null}
      {open === "disable" ? (
        <DisableDialog
          onClose={() => setOpen(null)}
          onDisabled={() => {
            setOpen(null);
            setNotice("Two-step verification is off.");
            refresh();
          }}
        />
      ) : null}
    </div>
  );
}
