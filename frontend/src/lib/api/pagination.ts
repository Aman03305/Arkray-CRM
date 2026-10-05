/** The cursor inside a `next`/`previous` link (the link's host is never used). */
export function cursorOf(link: string | null | undefined): string | null {
  if (!link) return null;
  try {
    return new URL(link, "https://arkray.invalid").searchParams.get("cursor");
  } catch {
    return null;
  }
}
