"use client";

import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Search, UsersRound } from "lucide-react";
import { useEffect, useId, useState } from "react";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { EmptyState } from "@/components/ui/EmptyState";
import { PageHeader } from "@/components/ui/PageHeader";
import { describeError, isApiError } from "@/lib/api/errors";
import type { AdminUser } from "@/lib/api/types";
import { VIEWER_QUERY_KEY } from "@/lib/query-client";
import { useViewer } from "@/lib/viewer-context";

import {
  cursorOf,
  NO_FILTERS,
  ROLE_OPTIONS,
  SEARCH_MIN_LENGTH,
  STATUS_OPTIONS,
  type UserFilters,
  USERS_QUERY_KEY,
  usersApi,
} from "./api";
import { ChangeEmailDialog } from "./ChangeEmailDialog";
import { LifecycleDialog } from "./LifecycleDialog";
import { UserFormDialog } from "./UserFormDialog";
import { type UserAction, UsersTable } from "./UsersTable";

type OpenDialog =
  | { kind: "create" }
  | { kind: "edit"; user: AdminUser }
  | { kind: "change-email"; user: AdminUser }
  | { kind: "deactivate" | "activate" | "resend"; user: AdminUser }
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

export function UsersPage() {
  const viewer = useViewer();
  const queryClient = useQueryClient();
  const [filters, setFilters] = useState<UserFilters>(NO_FILTERS);
  const [cursor, setCursor] = useState<string | null>(null);
  const [dialog, setDialog] = useState<OpenDialog>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const searchId = useId();
  const statusId = useId();
  const roleId = useId();

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
  const filtered = Boolean(effective.q || filters.status || filters.role);

  const onSaved = (user: AdminUser, message: string) => {
    setDialog(null);
    setNotice(message);
    void queryClient.invalidateQueries({ queryKey: USERS_QUERY_KEY });
    // Edited yourself: the sidebar and Settings show your details too.
    if (user.id === viewer?.id) void queryClient.invalidateQueries({ queryKey: VIEWER_QUERY_KEY });
  };

  const onAction = (action: UserAction, user: AdminUser) => {
    setNotice(null);
    setDialog(action === "edit" ? { kind: "edit", user } : { kind: action, user });
  };

  const rows = users.data?.results;
  const error = users.isError ? describeError(users.error) : null;

  return (
    <>
      <PageHeader
        title="Users"
        subtitle="Create and manage CRM users. Click a name to open their CRM."
        actions={
          <Button icon={<Plus aria-hidden="true" className="size-4" />} onClick={() => setDialog({ kind: "create" })}>
            New user
          </Button>
        }
      />

      <div role="search" className="mb-4 flex flex-wrap items-end gap-3">
        <div className="relative min-w-56 flex-1 sm:max-w-sm">
          <label htmlFor={searchId} className="sr-only">
            Search users
          </label>
          <Search aria-hidden="true" className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-slate-400" />
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
      </div>
      {filters.q.trim().length > 0 && filters.q.trim().length < SEARCH_MIN_LENGTH ? (
        <p className="-mt-2 mb-4 text-xs text-slate-500">Type at least {SEARCH_MIN_LENGTH} characters to search.</p>
      ) : null}

      <div aria-live="polite" className="mb-4 empty:hidden">
        {notice ? <Alert tone="success">{notice}</Alert> : null}
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
          description={filtered ? "Try a different search or clear the filters." : "Create the first user to invite them to Arkray CRM."}
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
          <UsersTable users={rows} loading={users.isPending} viewerId={viewer?.id} onAction={onAction} />
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
          onSaved={onSaved}
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
    </>
  );
}
