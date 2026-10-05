"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { LifeBuoy } from "lucide-react";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/Button";
import { SECURITY_EVENTS_QUERY_KEY, supportApi } from "@/features/users/api";
import { describeError } from "@/lib/api/errors";
import { setFlash } from "@/lib/flash";
import { supportSessionPath } from "@/lib/navigation";
import { VIEWER_QUERY_KEY } from "@/lib/query-client";
import type { SupportSession, Viewer } from "@/lib/viewer";
import { useViewer } from "@/lib/viewer-context";

const MINUTE = 60_000;
/** After the expiry time, how often to ask again whether the session is really over. */
const RECHECK = 30_000;
export const USERS_HOME = "/admin/users";
export const SESSION_ENDED = "Support session ended";

/**
 * Whole minutes left (rounded up), re-rendering only when that number changes. Once the
 * time is up it calls `onExpired`, again every half minute until the session is gone.
 */
function useMinutesLeft(expiresAt: string, onExpired: () => void): number {
  const [now, setNow] = useState(() => Date.now());
  const remaining = Date.parse(expiresAt) - now;
  const expired = useRef(onExpired);
  useEffect(() => {
    expired.current = onExpired;
  });
  useEffect(() => {
    if (remaining <= 0) expired.current();
    const wait = remaining > 0 ? remaining % MINUTE || MINUTE : RECHECK;
    const timer = setTimeout(() => setNow(Date.now()), wait);
    return () => clearTimeout(timer);
  }, [remaining]);
  return Math.max(0, Math.ceil(remaining / MINUTE));
}

function Banner({ session, actor, onExited }: { session: SupportSession; actor: string; onExited: () => void }) {
  const queryClient = useQueryClient();
  const router = useRouter();
  const exit = useMutation({
    mutationFn: supportApi.exit,
    onSuccess: () => {
      onExited();
      queryClient.setQueryData<Viewer>(VIEWER_QUERY_KEY, (current) =>
        current ? { ...current, supportSession: null } : current,
      );
      void queryClient.invalidateQueries({ queryKey: VIEWER_QUERY_KEY });
      void queryClient.invalidateQueries({ queryKey: SECURITY_EVENTS_QUERY_KEY });
      router.push(USERS_HOME);
    },
  });
  const exitError = exit.isError ? describeError(exit.error) : null;
  // Time's up: the API decides whether the session is over (the shell then follows it).
  const minutes = useMinutesLeft(session.expiresAt, () => {
    void queryClient.invalidateQueries({ queryKey: VIEWER_QUERY_KEY }, { cancelRefetch: false });
  });
  return (
    <section
      aria-label="Support session"
      className="sticky top-12 z-10 border-b border-amber-300 bg-amber-100 px-4 py-2 text-amber-950 lg:px-6"
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <LifeBuoy aria-hidden="true" className="size-4 shrink-0" />
        <div className="min-w-0 flex-1">
          <p className="text-sm [overflow-wrap:anywhere]">
            <span className="font-semibold">Support session</span>
            <span aria-hidden="true"> · </span>
            <span>{session.target.fullName}</span>
            <span aria-hidden="true"> · </span>
            <span>{minutes > 0 ? `${minutes} min left` : "Ending…"}</span>
          </p>
          <p className="text-xs text-amber-900">Signed in as {actor}</p>
        </div>
        <Button variant="secondary" size="sm" onClick={() => exit.mutate()} loading={exit.isPending || exit.isSuccess}>
          Exit
        </Button>
      </div>
      {exitError ? (
        <p role="alert" className="mt-1 text-xs text-red-800">
          Couldn&apos;t exit the support session. {exitError.message}
        </p>
      ) : null}
    </section>
  );
}

/**
 * Everything the shell does for a support session (an administrator working in one user's
 * CRM, signed in as themselves): the banner with the time left and Exit; keeping every page
 * inside that user's workspace; and noticing the end (Exit, expiry, ended elsewhere), after
 * which the administrator is back on Users. Mounted by AppShell from the first session on,
 * so it is still there to see the session end.
 */
export function SupportSessionBar() {
  const viewer = useViewer();
  const session = viewer?.supportSession ?? null;
  const pathname = usePathname();
  const router = useRouter();
  const exited = useRef(false);

  const redirect = session ? supportSessionPath(pathname, session) : null;
  useEffect(() => {
    if (redirect) router.replace(redirect);
  }, [redirect, router]);

  // Ended without Exit (expired, or ended elsewhere): say so on Users and go there.
  const sessionId = session?.id ?? null;
  const previous = useRef(sessionId);
  useEffect(() => {
    const was = previous.current;
    previous.current = sessionId;
    if (sessionId !== null) {
      exited.current = false;
      return;
    }
    if (was === null || exited.current) return;
    setFlash(SESSION_ENDED, USERS_HOME);
    router.push(USERS_HOME);
  }, [sessionId, router]);

  // Announced politely once the banner is on screen (a region that appears with its text
  // already in it is often not read).
  const announcer = useRef<HTMLParagraphElement>(null);
  const target = session?.target.fullName;
  useEffect(() => {
    if (announcer.current) announcer.current.textContent = target ? `Support session started for ${target}` : "";
  }, [target]);

  return (
    <>
      <p ref={announcer} aria-live="polite" className="sr-only" />
      {session && viewer ? (
        <Banner
          key={session.id}
          session={session}
          actor={viewer.fullName}
          onExited={() => {
            exited.current = true;
          }}
        />
      ) : null}
    </>
  );
}
