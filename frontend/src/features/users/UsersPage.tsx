"use client";

import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Search, UsersRound } from "lucide-react";
import { useCallback, useEffect, useId, useState } from "react";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { EmptyState } from "@/components/ui/EmptyState";
import { PageHeader } from "@/components/ui/PageHeader";
import { ExportDataDialog } from "@/features/privacy/ExportDataDialog";
import { PseudonymiseDialog } from "@/features/privacy/PseudonymiseDialog";
import { workspaceKeys } from "@/features/workspace/api";
import { describeError, isApiError } from "@/lib/api/errors";
import type { AdminUser } from "@/lib/api/types";
import { useFlash } from "@/lib/flash";
import { usersPageLabel } from "@/lib/navigation";
import { VIEWER_QUERY_KEY } from "@/lib/query-client";
import { canManageAdmins } from "@/lib/viewer";
import { useViewer } from "@/lib/viewer-context";

import {
  cursorOf,
  NO_FILTERS,
  ROLE_OPTIONS,
  SEARCH_MIN_LENGTH,
  SECURITY_EVENTS_QUERY_KEY,
  STATUS_OPTIONS,
  type UserFilters,
  userDetailKey,
  USERS_QUERY_KEY,
  usersApi,
} from "./api";
import { AccessAsUserDialog } from "./AccessAsUserDialog";
import { AdminFilter } from "./AdminFilter";
import { ChangeEmailDialog } from "./ChangeEmailDialog";
import { LifecycleDialog } from "./LifecycleDialog";
import { ClearSignInLockDialog } from "./ClearSignInLockDialog";
import { ResetMfaDialog } from "./ResetMfaDialog";
import { SetPasswordDialog } from "./SetPasswordDialog";
import { OutOfReachProvider } from "./reach";
import { UserDetailsDrawer } from "./UserDetailsDrawer";
import { UserFormDialog } from "./UserFormDialog";
import { type UserAction, UsersTable } from "./UsersTable";

type OpenDialog =
  | { kind: "create" }
  // From the details panel (role or Admin): saving goes back to it, with the outcome.
  | { kind: "edit"; user: AdminUser; fromDetails?: boolean }
  | { kind: "change-email"; user: AdminUser }
  | { kind: "deactivate" | "activate" | "resend"; user: AdminUser }
  | { kind: "reset-mfa" | "clear-sign-in-lock"; user: AdminUser }
  | { kind: "set-password" | "support"; user: AdminUser }
  | { kind: "export" | "pseudonymise"; user: AdminUser }
  | null;

function useDebounced<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delayMs);
    return () => clearTimeout(timer);
  }, [value, delayMs]);
  return debounced;
}

const FILTER_CONTROL =
  "h-9 rounded-md border border-slate-300 bg-white px-3 text-sm text-slate-900 focus-visible:outline-brand-600";

/**
 * The Users page. A Super admin manages everyone: roles, each User's Admin (their team) and
 * the administrators' accounts. An Admin (team administrator, ADR-0035) sees "My team":
 * themselves and the users they manage, and creates Users only, into their team. The API
 * lists only who the viewer may reach, and refuses everything else.
 */
export function UsersPage() {
  const viewer = useViewer();
  const superAdmin = canManageAdmins(viewer);
  const queryClient = useQueryClient();
  const [filters, setFilters] = useState<UserFilters>(NO_FILTERS);
  const [cursor, setCursor] = useState<string | null>(null);
  const [dialog, setDialog] = useState<OpenDialog>(null);
  // The user whose details panel is open. A dialog opened from it takes its place (no
  // stacked modals); closing the dialog brings the panel back.
  const [details, setDetails] = useState<AdminUser | null>(null);
  const [detailsNotice, setDetailsNotice] = useState<string | null>(null);
  // Also carries a notice from elsewhere, e.g. "Support session ended" (lib/flash).
  const [notice, setNotice] = useFlash();
  const searchId = useId();
  const statusId = useId();
  const roleId = useId();
  // Someone the API no longer lets the viewer reach (404): what was open about them closed.
  const [gone, setGone] = useState<string | null>(null);

  const q = useDebounced(filters.q, 300);
  const effective: UserFilters = { ...filters, q: q.trim().length >= SEARCH_MIN_LENGTH ? q.trim() : "" };
  const users = useQuery({
    queryKey: [...USERS_QUERY_KEY, effective, cursor],
    queryFn: () => usersApi.list(effective, cursor),
    placeholderData: keepPreviousData,
  });

  const updateFilters = (patch: Partial<UserFilters>) => {
    setFilters((f) => ({ ...f, ...patch }));
    setCursor(null);
  };
  const filtered = Boolean(effective.q || filters.status || filters.role || filters.manager);

  const refresh = (user: AdminUser) => {
    queryClient.setQueryData(userDetailKey(user.id), user);
    void queryClient.invalidateQueries({ queryKey: USERS_QUERY_KEY });
    void queryClient.invalidateQueries({ queryKey: SECURITY_EVENTS_QUERY_KEY });
    // Their workspace banner shows their name and status (deactivated, reactivated, renamed).
    void queryClient.invalidateQueries({ queryKey: workspaceKeys.subject(user.id) });
    // Edited yourself: the sidebar and Settings show your details too.
    if (user.id === viewer?.id) void queryClient.invalidateQueries({ queryKey: VIEWER_QUERY_KEY });
  };

  const onSaved = (user: AdminUser, message: string) => {
    setDialog(null);
    setNotice(message);
    refresh(user);
  };

  // Saved from the details panel: back to it, which shows the outcome (like a new password).
  const onSavedToDetails = (user: AdminUser, message: string) => {
    setDialog(null);
    setDetailsNotice(message);
    refresh(user);
  };

  const onPasswordSet = (user: AdminUser, message: string) => {
    setDialog(null); // back to the details panel, which shows the outcome
    setDetailsNotice(message);
    refresh(user);
  };

  // A 404 about a listed user (their details, or an action on them): they are out of the
  // viewer's reach now, e.g. a team member moved to another team (ADR-0035; out of reach
  // reads exactly like an account that doesn't exist). Nothing about them stays open, the
  // page says why, and the list is read again.
  const onOutOfReach = useCallback(
    (user: { id: string; fullName: string }) => {
      setDialog(null);
      setDetails(null);
      setDetailsNotice(null);
      setNotice(null);
      setGone(
        `${user.fullName} is no longer in your reach (they may have moved to another team). Nothing was changed, and the list has been refreshed.`,
      );
      queryClient.removeQueries({ queryKey: userDetailKey(user.id) });
      void queryClient.invalidateQueries({ queryKey: USERS_QUERY_KEY });
    },
    [queryClient, setNotice],
  );

  const onAction = (action: UserAction, user: AdminUser) => {
    setNotice(null);
    setGone(null);
    setDetails(null);
    setDialog(action === "edit" ? { kind: "edit", user } : { kind: action, user });
  };

  const openDetails = (user: AdminUser) => {
    setNotice(null);
    setGone(null);
    setDetailsNotice(null);
    setDetails(user);
  };

  const rows = users.data?.results;
  const error = users.isError ? describeError(users.error) : null;

  return (
    <OutOfReachProvider value={onOutOfReach}>
      <PageHeader
        title={usersPageLabel(viewer)}
        subtitle={superAdmin ? undefined : "You and the users you manage."}
        actions={
          <Button
            icon={<Plus aria-hidden="true" className="size-4" />}
            onClick={() => {
              setGone(null);
              setDialog({ kind: "create" });
            }}
          >
            New user
          </Button>
        }
      />

      <div role="search" className="mb-4 flex flex-wrap items-end gap-3">
        <div className="relative min-w-56 flex-1 sm:max-w-sm">
          <label htmlFor={searchId} className="sr-only">
            Search users
          </label>
          <Search aria-hidden="true" className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-slate-500" />
          <input
            id={searchId}
            type="search"
            placeholder="Search by name or email"
            maxLength={100}
            value={filters.q}
            onChange={(e) => updateFilters({ q: e.target.value })}
            className={`${FILTER_CONTROL} w-full pl-9`}
          />
        </div>
        <div>
          <label htmlFor={statusId} className="sr-only">
            Filter by status
          </label>
          <select
            id={statusId}
            value={filters.status}
            onChange={(e) => updateFilters({ status: e.target.value as UserFilters["status"] })}
            className={FILTER_CONTROL}
          >
            <option value="">All statuses</option>
            {STATUS_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </div>
        {superAdmin ? (
          <div>
            <label htmlFor={roleId} className="sr-only">
              Filter by role
            </label>
            <select
              id={roleId}
              value={filters.role}
              onChange={(e) => updateFilters({ role: e.target.value as UserFilters["role"] })}
              className={FILTER_CONTROL}
            >
              <option value="">All roles</option>
              {ROLE_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </div>
        ) : null}
        {superAdmin ? (
          <AdminFilter value={filters.manager} onChange={(manager) => updateFilters({ manager })} className={FILTER_CONTROL} />
        ) : null}
      </div>
      {filters.q.trim().length > 0 && filters.q.trim().length < SEARCH_MIN_LENGTH ? (
        <p className="-mt-2 mb-4 text-xs text-slate-500">Type at least {SEARCH_MIN_LENGTH} characters to search.</p>
      ) : null}

      <div aria-live="polite" className="mb-4 empty:hidden">
        {notice ? <Alert tone="success">{notice}</Alert> : null}
        {gone ? <Alert tone="info">{gone}</Alert> : null}
      </div>

      {error ? (
        <Alert
          tone="error"
          title={isApiError(users.error, 403) ? "You can't manage users" : "Users couldn't be loaded"}
          requestId={error.requestId}
          action={
            isApiError(users.error, 403) ? null : (
              <Button variant="secondary" size="sm" onClick={() => void users.refetch()} loading={users.isFetching}>
                Try again
              </Button>
            )
          }
        >
          {error.message}
        </Alert>
      ) : rows && rows.length === 0 ? (
        <EmptyState
          icon={UsersRound}
          title={filtered ? "No users match your filters" : "No users yet"}
          description={filtered ? "Try a different search or clear the filters." : undefined}
          action={
            filtered ? (
              <Button variant="secondary" onClick={() => updateFilters(NO_FILTERS)}>
                Clear filters
              </Button>
            ) : null
          }
        />
      ) : (
        <>
          <UsersTable users={rows} loading={users.isPending} viewerId={viewer?.id} onAction={onAction} onOpen={openDetails} />
          <nav aria-label="Pagination" className="mt-4 flex items-center justify-end gap-2">
            <Button
              variant="secondary"
              size="sm"
              disabled={!users.data?.previous || users.isFetching}
              onClick={() => setCursor(cursorOf(users.data?.previous))}
            >
              Previous
            </Button>
            <Button
              variant="secondary"
              size="sm"
              disabled={!users.data?.next || users.isFetching}
              onClick={() => setCursor(cursorOf(users.data?.next))}
            >
              Next
            </Button>
          </nav>
        </>
      )}

      {dialog?.kind === "create" ? (
        <UserFormDialog mode={{ kind: "create" }} onClose={() => setDialog(null)} onSaved={onSaved} />
      ) : null}
      {dialog?.kind === "edit" ? (
        <UserFormDialog
          mode={{ kind: "edit", user: dialog.user, isSelf: dialog.user.id === viewer?.id }}
          onClose={() => setDialog(null)}
          onSaved={dialog.fromDetails ? onSavedToDetails : onSaved}
        />
      ) : null}
      {dialog?.kind === "change-email" ? (
        <ChangeEmailDialog
          user={dialog.user}
          isSelf={dialog.user.id === viewer?.id}
          onClose={() => setDialog(null)}
          onSaved={onSaved}
        />
      ) : null}
      {dialog && (dialog.kind === "deactivate" || dialog.kind === "activate" || dialog.kind === "resend") ? (
        <LifecycleDialog action={dialog.kind} user={dialog.user} onClose={() => setDialog(null)} onDone={onSaved} />
      ) : null}
      {dialog?.kind === "reset-mfa" ? (
        <ResetMfaDialog user={dialog.user} onClose={() => setDialog(null)} onDone={onSaved} />
      ) : null}
      {dialog?.kind === "clear-sign-in-lock" ? (
        <ClearSignInLockDialog user={dialog.user} onClose={() => setDialog(null)} onDone={onSaved} />
      ) : null}
      {dialog?.kind === "set-password" ? (
        <SetPasswordDialog user={dialog.user} onClose={() => setDialog(null)} onDone={onPasswordSet} />
      ) : null}
      {dialog?.kind === "support" ? <AccessAsUserDialog user={dialog.user} onClose={() => setDialog(null)} /> : null}
      {dialog?.kind === "export" ? (
        <ExportDataDialog subject={{ type: "user", id: dialog.user.id, name: dialog.user.full_name }} onClose={() => setDialog(null)} />
      ) : null}
      {dialog?.kind === "pseudonymise" ? <PseudonymiseDialog user={dialog.user} onClose={() => setDialog(null)} /> : null}
      {details && dialog === null ? (
        <UserDetailsDrawer
          user={details}
          notice={detailsNotice}
          onClose={() => {
            setDetails(null);
            setDetailsNotice(null);
          }}
          onEdit={(user) => {
            setDetailsNotice(null);
            setDialog({ kind: "edit", user, fromDetails: true });
          }}
          onSetPassword={(user) => {
            setDetailsNotice(null);
            setDialog({ kind: "set-password", user });
          }}
          onAccess={(user) => {
            setDetailsNotice(null);
            setDialog({ kind: "support", user });
          }}
          onExport={(user) => {
            setDetailsNotice(null);
            setDialog({ kind: "export", user });
          }}
          onPseudonymise={(user) => {
            setDetailsNotice(null);
            setDialog({ kind: "pseudonymise", user });
          }}
        />
      ) : null}
    </OutOfReachProvider>
  );
}
