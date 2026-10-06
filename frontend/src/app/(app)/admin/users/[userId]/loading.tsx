import { PageSkeleton } from "@/components/ui/PageSkeleton";

// Moving between modules of a user's workspace: the banner stays, the module loads below it
// (see app/(app)/loading.tsx).
export default function Loading() {
  return <PageSkeleton />;
}
