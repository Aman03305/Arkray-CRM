"use client";

import { ErrorView } from "@/components/ui/ErrorView";

import "./globals.css";

/**
 * The root layout itself failed (ARCH-10). This replaces it, so it brings its own document
 * and stylesheet (Next.js doesn't apply the root layout's). No providers: ErrorView needs
 * none. Error boundaries are client components, so no `metadata` export: React's <title>.
 */
export default function GlobalError({ error, retry }: { error: Error & { digest?: string }; retry: () => void }) {
  return (
    <html lang="en">
      <body>
        <title>Something went wrong · Arkray CRM</title>
        <main>
          <ErrorView fullPage digest={error.digest} onRetry={retry} />
        </main>
      </body>
    </html>
  );
}
