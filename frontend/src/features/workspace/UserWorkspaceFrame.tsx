"use client";

import { usePathname, useRouter } from "next/navigation";
import { type ReactNode, useEffect, useRef } from "react";

import { WorkspaceBanner } from "@/components/shell/WorkspaceBanner";
import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { NotFoundView } from "@/components/ui/NotFoundView";
import { PageHeader } from "@/components/ui/PageHeader";
import { Skeleton } from "@/components/ui/Skeleton";
import { describeError, isApiError } from "@/lib/api/errors";
import { hasCapability } from "@/lib/viewer";
import { useViewer } from "@/lib/viewer-context";
import { canonicalUserPath, userIdFromPathname } from "@/lib/workspace";

import { useWorkspaceSubject } from "./api";

/**
 * An administrator viewing one user's CRM (docs/admin-user-workspace.md). Opening it calls
 * GET /api/v1/workspaces/{id}, which authorises the access and writes the
 * `workspace.accessed` audit event; the banner then names whose CRM this is. The admin stays
 * signed in as themselves throughout.
 *
 * The module views below read their workspace from the URL, the banner from this layout's
 * `userId`. They are rendered only when both name the same user: a URL this frame can't
 * reconcile shows "not found", never another workspace's records under this user's name.
 * A non-canonical spelling of the id (upper case, percent-encoded) is replaced by the
 * canonical URL first, so every workspace has exactly one address.
 *
 * In a support session for this user the shell's support banner already says whose CRM this
 * is and who is signed in, so this frame adds no banner of its own (never two).
 */
export function UserWorkspaceFrame({ userId, children }: { userId: string; children: ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const viewer = useViewer();
  const subject = useWorkspaceSubject(userId);
  const sameUser = userIdFromPathname(pathname) === userId;
  const canonical = sameUser ? canonicalUserPath(pathname) : null;

  useEffect(() => {
    if (canonical) router.replace(`${canonical}${window.location.search}${window.location.hash}`);
  }, [canonical, router]);

  // Page titles don't name the user (they'd land in the browser history), so screen readers
  // hear whose CRM opened from a polite live region: rendered empty, then filled once the
  // API has named the user (a region that appears with its text already in it is silent).
  const name = subject.data?.full_name;
  const announcer = useRef<HTMLParagraphElement>(null);
  useEffect(() => {
    if (name && announcer.current) announcer.current.textContent = `Viewing CRM for ${name}`;
  }, [name]);

  if (!sameUser || isApiError(subject.error, 404)) return <NotFoundView />;
  if (canonical) {
    return (
      <div aria-busy="true">
        <Skeleton className="h-12 w-full" />
        <span className="sr-only">Loading</span>
      </div>
    );
  }
  // A failed first load shows the error instead of the workspace (fail closed). Once the
  // user is known, a failed background refresh must not unmount the page (and any unsaved
  // form) beneath the banner; the user's identity can't have changed meanwhile.
  if (subject.isError && subject.data === undefined) {
    const { message, requestId } = describeError(subject.error);
    return (
      <>
        <PageHeader title="User workspace" />
        <Alert
          tone="error"
          title="This workspace couldn't be opened"
          requestId={requestId}
          action={
            <Button variant="secondary" size="sm" onClick={() => void subject.refetch()} loading={subject.isFetching}>
              Try again
            </Button>
          }
        >
          {message}
        </Alert>
      </>
    );
  }
  if (viewer?.supportSession?.target.id === userId) return <>{children}</>;
  const manager = hasCapability(viewer, "users.manage");
  return (
    <>
      <p ref={announcer} aria-live="polite" className="sr-only" />
      <WorkspaceBanner
        subject={subject.data}
        actor={viewer}
        back={manager ? { href: "/admin/users", label: "Back to Users" } : { href: "/dashboard", label: "Back to Dashboard" }}
      />
      {children}
    </>
  );
}
