import type { ReactNode } from "react";

import { AppShell } from "@/components/shell/AppShell";
import { SessionGate } from "@/features/auth/SessionGate";

/*
 * Authenticated application shell. SessionGate loads the signed-in user from
 * GET /api/v1/auth/me; without a session the browser is sent to /login. That redirect is
 * UX only: the API rejects unauthenticated requests anyway.
 */
export default function AppLayout({ children }: { children: ReactNode }) {
  return (
    <SessionGate>
      <AppShell>{children}</AppShell>
    </SessionGate>
  );
}
