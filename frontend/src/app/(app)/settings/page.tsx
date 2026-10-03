import type { Metadata } from "next";

import { ProfilePage } from "@/features/profile/ProfilePage";

export const metadata: Metadata = { title: "Settings" };

export default function SettingsPage() {
  return <ProfilePage />;
}
