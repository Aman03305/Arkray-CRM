"use client";

import { ErrorView } from "@/components/ui/ErrorView";

/**
 * Everything below the root layout that has no nearer boundary (ARCH-10): the sign-in pages,
 * and the signed-in shell itself (session gate, header, navigation). A full page of its own,
 * with its own main landmark, as the root not-found page.
 */
export default function RootError({ error, retry }: { error: Error & { digest?: string }; retry: () => void }) {
  return (
    <main>
      <ErrorView fullPage digest={error.digest} onRetry={retry} />
    </main>
  );
}
