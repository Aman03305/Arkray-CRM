import type { UserRef } from "@/lib/api/types";

export function PersonName({ person }: { person: UserRef }) {
  return (
    <>
      {person.full_name}
      {person.is_active ? null : <span className="ml-1 text-xs text-slate-500">(deactivated)</span>}
    </>
  );
}
