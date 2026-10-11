import type { Metadata } from "next";
import { cookies } from "next/headers";
import { connection } from "next/server";
import type { ReactNode } from "react";

import { SESSION_COOKIE_NAMES } from "@/lib/api/client";

import { Providers } from "./providers";

import "./globals.css";

export const metadata: Metadata = {
  title: { default: "Arkray CRM", template: "%s · Arkray CRM" },
  description: "Arkray sales CRM",
  robots: { index: false, follow: false },
};

export default async function RootLayout({ children }: Readonly<{ children: ReactNode }>) {
  // Render per request: the Content-Security-Policy nonce (src/proxy.ts) can only be put on
  // Next.js's scripts at request time, never into prerendered HTML.
  await connection();
  // Whether the browser sent a session cookie (its presence only, never its value): a 401
  // on this page then means the session ended, and the sign-in page says so, even on a
  // freshly loaded page that never saw the signed-in user (E2E-1, lib/query-client).
  const cookieStore = await cookies();
  const sessionAtLoad = SESSION_COOKIE_NAMES.some((name) => cookieStore.has(name));
  return (
    <html lang="en">
      <body>
        <Providers sessionAtLoad={sessionAtLoad}>{children}</Providers>
      </body>
    </html>
  );
}
