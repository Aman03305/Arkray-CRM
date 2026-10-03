import type { Metadata } from "next";
import { Suspense } from "react";

import { AuthCard } from "@/features/auth/AuthCard";
import { LoginForm } from "@/features/auth/LoginForm";

export const metadata: Metadata = { title: "Sign in" };

export default function LoginPage() {
  return (
    <Suspense fallback={<AuthCard title="Sign in">{null}</AuthCard>}>
      <LoginForm />
    </Suspense>
  );
}
