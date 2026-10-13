"use client";

import { createContext, useContext, useEffect } from "react";

import { isApiError } from "@/lib/api/errors";

type OutOfReach = (user: { id: string; fullName: string }) => void;

const OutOfReachContext = createContext<OutOfReach | null>(null);

/** Provided by the Users page: closes whatever is open about the user and says why. */
export const OutOfReachProvider = OutOfReachContext.Provider;

/**
 * Reports a 404 about `user` (loading their details, or any action on them) to the Users
 * page: the API no longer lets the viewer reach them, e.g. a team member moved to another
 * team or their Admin was changed (ADR-0035; out of reach is the same 404 as an account that
 * doesn't exist). Their panel or dialog must not stay open with actions that can only fail.
 */
export function useOutOfReachOn404(error: unknown, user: { id: string; full_name: string }): void {
  const report = useContext(OutOfReachContext);
  const gone = isApiError(error, 404);
  const { id, full_name: fullName } = user;
  useEffect(() => {
    if (gone) report?.({ id, fullName });
  }, [gone, report, id, fullName]);
}
