import Link from "next/link";

/**
 * One view for "does not exist" and "not yours": like the API's 404s, it never reveals
 * whether something exists but is off-limits.
 */
export function NotFoundView({ fullPage = false }: { fullPage?: boolean }) {
  return (
    <div className={`flex flex-col items-center justify-center gap-3 px-4 text-center ${fullPage ? "min-h-dvh" : "py-24"}`}>
      <p className="text-sm font-medium text-brand-600">404</p>
      <h1 className="text-xl font-semibold">Page not found</h1>
      <p className="text-sm text-slate-500">The page does not exist or you do not have access to it.</p>
      <Link href="/dashboard" className="mt-2 text-sm font-medium text-brand-700 hover:underline">
        Go to Dashboard
      </Link>
    </div>
  );
}
