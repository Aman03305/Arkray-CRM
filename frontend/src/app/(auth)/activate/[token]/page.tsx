import type { Metadata } from "next";

import { ActivateAccountForm } from "@/features/auth/ActivateAccountForm";

export const metadata: Metadata = { title: "Activate your account", referrer: "no-referrer" };

export default async function ActivatePage({ params }: PageProps<"/activate/[token]">) {
  const { token } = await params;
  return <ActivateAccountForm token={token} />;
}
