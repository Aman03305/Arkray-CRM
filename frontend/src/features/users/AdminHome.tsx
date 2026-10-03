"use client";

import { useQuery } from "@tanstack/react-query";
import { ChartNoAxesColumn, UsersRound } from "lucide-react";
import Link from "next/link";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { PageHeader } from "@/components/ui/PageHeader";
import { Skeleton } from "@/components/ui/Skeleton";
import { describeError } from "@/lib/api/errors";
import { hasCapability } from "@/lib/viewer";
import { useViewer } from "@/lib/viewer-context";
import { workspaceHref } from "@/lib/workspace";

import { NO_FILTERS, USERS_QUERY_KEY, usersApi } from "./api";
import { UserStatus } from "./UserStatus";

const RECENT = 5;

function RecentUsers() {
  const users = useQuery({
    queryKey: [...USERS_QUERY_KEY, "recent"],
    queryFn: () => usersApi.list(NO_FILTERS, null, RECENT),
  });

  if (users.isError) {
    const { message, requestId } = describeError(users.error);
    return (
      <Alert
        tone="error"
        requestId={requestId}
        action={
          <Button variant="secondary" size="sm" onClick={() => void users.refetch()} loading={users.isFetching}>
            Try again
          </Button>
        }
      >
        {message}
      </Alert>
    );
  }
  if (users.isPending) {
    return (
      <ul aria-busy="true" className="divide-y divide-slate-100">
        {Array.from({ length: 3 }, (_, i) => (
          <li key={i} className="py-3">
            <Skeleton className="h-4 w-48" />
          </li>
        ))}
        <li className="sr-only">Loading users</li>
      </ul>
    );
  }
  return (
    <ul className="divide-y divide-slate-100">
      {users.data.results.map((user) => (
        <li key={user.id} className="flex items-center justify-between gap-4 py-3">
          <div className="min-w-0">
            <Link
              href={workspaceHref({ kind: "user", userId: user.id }, "dashboard")}
              className="block truncate text-sm font-medium text-slate-900 hover:text-brand-700 hover:underline"
            >
              {user.full_name}
            </Link>
            <span className="block truncate text-xs text-slate-500">
              {user.email} · {user.role_label}
            </span>
          </div>
          <UserStatus user={user} />
        </li>
      ))}
    </ul>
  );
}

/**
 * The administrator's home: the organisation-wide dashboard (ADR-0010). CRM figures only
 * appear once the modules that produce them exist; nothing here is invented.
 */
export function AdminHome() {
  // The organisation dashboard needs crm.view_all; the users panel needs users.manage.
  const canManageUsers = hasCapability(useViewer(), "users.manage");
  return (
    <>
      <PageHeader title="Dashboard" subtitle="Organization overview" />
      <div className="grid gap-6 lg:grid-cols-5">
        <section aria-labelledby="org-figures" className="rounded-lg border border-slate-200 bg-white p-5 lg:col-span-3">
          <h2 id="org-figures" className="text-sm font-semibold text-slate-900">
            Organization figures
          </h2>
          <div className="mt-6 flex flex-col items-center py-8 text-center">
            <span className="mb-3 flex size-10 items-center justify-center rounded-full bg-slate-100">
              <ChartNoAxesColumn aria-hidden="true" className="size-5 text-slate-500" />
            </span>
            <p className="text-sm font-medium text-slate-900">No figures yet</p>
            <p className="mt-1 max-w-sm text-sm text-slate-500">
              Leads, pipeline and activity figures appear here once those modules are in use.
            </p>
          </div>
        </section>
        {canManageUsers ? (
        <section aria-labelledby="recent-users" className="rounded-lg border border-slate-200 bg-white p-5 lg:col-span-2">
          <div className="flex items-center justify-between gap-2">
            <h2 id="recent-users" className="flex items-center gap-2 text-sm font-semibold text-slate-900">
              <UsersRound aria-hidden="true" className="size-4 text-slate-400" />
              Recently added users
            </h2>
            <Link href="/admin/users" className="text-sm font-medium text-brand-700 hover:underline">
              Manage users
            </Link>
          </div>
          <div className="mt-2">
            <RecentUsers />
          </div>
        </section>
        ) : null}
      </div>
    </>
  );
}
