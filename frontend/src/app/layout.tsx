import type { Metadata } from "next";
import { connection } from "next/server";
import type { ReactNode } from "react";

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
  return (
    <html lang="en">
      <body>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
