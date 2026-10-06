import type { Metadata } from "next";
import { notFound } from "next/navigation";

/*
 * Every module of a user's workspace has its own route next to this one (dashboard,
 * pipeline, activities, ask), so each page loads only its own module's code. Any other
 * section is "not found", inside the workspace frame.
 */
export const metadata: Metadata = { title: "User workspace" };

export default function UnknownWorkspaceSection() {
  notFound();
}
