/** Arkray's mark. Decorative: the product name is always written next to it. */
export function BrandMark({ className = "size-7 text-sm" }: { className?: string }) {
  return (
    <span
      aria-hidden="true"
      className={`flex shrink-0 items-center justify-center rounded-md bg-brand-500 font-bold text-white shadow-[inset_0_-2px_0_rgba(0,0,0,0.15)] ${className}`}
    >
      A
    </span>
  );
}
