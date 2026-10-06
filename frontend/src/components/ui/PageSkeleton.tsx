import { Skeleton } from "./Skeleton";

/** A page still loading: placeholders where its header goes, announced as "Loading". */
export function PageSkeleton() {
  return (
    <div aria-busy="true" className="space-y-3">
      <Skeleton className="h-6 w-40" />
      <Skeleton className="h-4 w-64" />
      <span className="sr-only">Loading</span>
    </div>
  );
}
