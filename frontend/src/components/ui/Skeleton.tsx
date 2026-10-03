/** Loading placeholder. Hidden from assistive tech; pair it with an accessible label. */
export function Skeleton({ className = "" }: { className?: string }) {
  return <span aria-hidden="true" className={`inline-block animate-pulse rounded bg-slate-200 ${className}`} />;
}
