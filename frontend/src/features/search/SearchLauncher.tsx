"use client";

import { Search } from "lucide-react";
import { useEffect, useRef, useState, useSyncExternalStore } from "react";

import { useWorkspace } from "@/lib/use-workspace";
import { hasCapability } from "@/lib/viewer";
import { useViewer } from "@/lib/viewer-context";
import { type Workspace, workspaceApiSegment } from "@/lib/workspace";

import { SearchDialog } from "./SearchDialog";

const noSubscription = () => () => {};
const isApple = () => /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);

/**
 * Ctrl+K (⌘K on a Mac) opens search from anywhere, except:
 * - while another dialog is open (a form in a dialog keeps the keyboard), or the mobile
 *   navigation drawer;
 * - while typing in a multi-line field (a note being written), or an IME composition.
 * The visible Search button is always there too; the shortcut is never the only way in.
 */
function useSearchShortcut(enabled: boolean, isOpen: boolean, open: () => void): void {
  const openRef = useRef(open);
  useEffect(() => {
    openRef.current = open;
  });
  useEffect(() => {
    if (!enabled) return;
    const onKeyDown = (event: KeyboardEvent) => {
      // `code` too: on a non-Latin layout (Cyrillic, Devanagari InScript) K types another letter.
      if ((event.key.toLowerCase() !== "k" && event.code !== "KeyK") || !(event.ctrlKey || event.metaKey)) return;
      if (event.altKey || event.shiftKey || event.isComposing) return;
      if (isOpen) {
        event.preventDefault(); // already open: keep the browser's own Ctrl+K away
        return;
      }
      if (document.querySelector('[aria-modal="true"]')) return;
      const target = event.target;
      if (target instanceof HTMLTextAreaElement || (target instanceof HTMLElement && target.isContentEditable)) return;
      event.preventDefault();
      if (!event.repeat) openRef.current();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [enabled, isOpen]);
}

/**
 * The shell's Search entry. Remounted for every workspace (keyed by its API segment), so a
 * switch from one user's workspace to another's (Back, Forward, a link) closes the dialog
 * and drops everything typed or found: nothing searched in one workspace is ever shown in
 * another. Search is disabled until the workspace is certain: while the viewer is still
 * loading (it decides between one's own and the organisation's records; never a guessed
 * workspace, as with the module views), for a URL that names no workspace (a malformed user
 * id), and in a user's workspace the viewer may not open.
 */
export function SearchLauncher() {
  const viewer = useViewer();
  const found = useWorkspace();
  const workspace =
    viewer === null || (found?.kind === "user" && !hasCapability(viewer, "workspace.view_any")) ? null : found;
  return <WorkspaceSearch key={workspace ? workspaceApiSegment(workspace) : "none"} workspace={workspace} />;
}

function WorkspaceSearch({ workspace }: { workspace: Workspace | null }) {
  const [open, setOpen] = useState(false);
  const apple = useSyncExternalStore(noSubscription, isApple, () => false);
  useSearchShortcut(workspace !== null, open, () => setOpen(true));
  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        disabled={workspace === null}
        aria-haspopup="dialog"
        aria-keyshortcuts="Control+K Meta+K"
        className="flex h-8 min-w-0 max-w-md flex-1 items-center gap-2 rounded-full border border-white/10 bg-shell-raised px-3.5 text-left text-sm text-shell-muted transition-colors hover:border-white/25 hover:text-white focus:outline-none focus-visible:ring-2 focus-visible:ring-white disabled:cursor-not-allowed disabled:opacity-60"
      >
        <Search aria-hidden="true" className="size-4 shrink-0" />
        <span className="min-w-0 flex-1 truncate">
          Search<span className="hidden sm:inline"> deals, customers, activities</span>
        </span>
        <kbd aria-hidden="true" className="hidden shrink-0 rounded border border-white/15 bg-white/5 px-1.5 font-sans text-xs text-shell-muted sm:inline">
          {apple ? "⌘K" : "Ctrl K"}
        </kbd>
      </button>
      {open && workspace ? <SearchDialog workspace={workspace} onClose={() => setOpen(false)} /> : null}
    </>
  );
}
