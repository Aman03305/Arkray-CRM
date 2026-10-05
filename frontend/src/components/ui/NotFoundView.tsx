import Link from "next/link";

/**
 * One view for "does not exist" and "not yours": like the API's 404s, it never reveals
 * whether something exists but is off-limits. Inside a workspace the way back stays in that
 * workspace (e.g. back to the selected user's Pipeline), never the organisation's pages.
 */
export function NotFoundView({
  fullPage = false,
  back = { href: "/dashboard", label: "Go to Dashboard" },
}: {
  fullPage?: boolean;
  back?: { href: string; label: string };
}) {
  return (
    <div className={`flex flex-col items-center justify-center gap-3 px-4 text-center ${fullPage ? "min-h-dvh" : "py-24"}`}>
      <p className="text-sm font-medium text-brand-600">404</p>
      <h1 className="text-xl font-semibold">Page not found</h1>
      <p className="text-sm text-slate-500">The page does not exist or you do not have access to it.</p>
      <Link href={back.href} className="mt-2 text-sm font-medium text-brand-700 hover:underline">
        {back.label}
      </Link>
    </div>
  );
}
