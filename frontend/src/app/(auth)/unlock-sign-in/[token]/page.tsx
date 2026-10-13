import type { Metadata } from "next";

import { UnlockSignIn } from "@/features/auth/UnlockSignIn";

// The URL carries a one-time secret: never sent onwards in a Referer (as the reset and
// invitation pages; review of 73ff1e3, L1).
export const metadata: Metadata = { title: "Unlock sign-in", referrer: "no-referrer" };

export default async function UnlockSignInPage({ params }: PageProps<"/unlock-sign-in/[token]">) {
  const { token } = await params;
  return <UnlockSignIn token={token} />;
}
