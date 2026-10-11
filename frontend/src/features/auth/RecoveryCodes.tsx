"use client";

import { Copy, Download } from "lucide-react";
import { type FormEvent, useEffect, useId, useRef, useState } from "react";

import { Button } from "@/components/ui/Button";

export const RECOVERY_CODES_FILENAME = "arkray-crm-recovery-codes.txt";

export function recoveryCodesText(codes: readonly string[]): string {
  return [
    "Arkray CRM recovery codes",
    "Each code signs you in once, instead of a code from your authenticator app.",
    "",
    ...codes,
    "",
  ].join("\n");
}

/** Saves the codes as a text file through a temporary object URL (nothing leaves the browser). */
function downloadText(text: string, filename: string): void {
  const url = URL.createObjectURL(new Blob([text], { type: "text/plain;charset=utf-8" }));
  try {
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    link.rel = "noopener";
    document.body.append(link);
    link.click();
    link.remove();
  } finally {
    // After the click has started the download.
    setTimeout(() => URL.revokeObjectURL(url), 0);
  }
}

/**
 * The recovery codes, shown once (ADR-0033: the server keeps only their hashes): copy or
 * download them, and say they're saved before going on. The codes live only in this
 * component's props: nothing caches them, and they're gone once it closes.
 */
export function RecoveryCodes({ codes, onDone, doneLabel = "Continue" }: {
  codes: readonly string[];
  onDone: () => void;
  doneLabel?: string;
}) {
  const checkboxId = useId();
  const errorId = useId();
  const [saved, setSaved] = useState(false);
  const [unsaved, setUnsaved] = useState(false);
  const [copy, setCopy] = useState<"copied" | "failed" | null>(null);
  // They replace a form whose button is gone: start reading here, not from the page's top.
  const intro = useRef<HTMLDivElement>(null);
  useEffect(() => intro.current?.focus(), []);

  const onCopy = async () => {
    try {
      if (!navigator.clipboard) throw new Error("No clipboard");
      await navigator.clipboard.writeText(codes.join("\n"));
      setCopy("copied");
    } catch {
      setCopy("failed");
    }
  };

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    if (!saved) {
      setUnsaved(true);
      document.getElementById(checkboxId)?.focus();
      return;
    }
    onDone();
  };

  return (
    <form onSubmit={onSubmit} noValidate className="space-y-4">
      <div ref={intro} tabIndex={-1} className="rounded-md border border-slate-200 bg-slate-50 px-3.5 py-3 text-sm text-slate-700 focus:outline-none">
        <p className="font-medium text-slate-900">Save your recovery codes</p>
        <p className="mt-0.5">
          If you lose your phone, each code signs you in once instead of the app. Keep them somewhere safe, like a
          password manager. They won&apos;t be shown again.
        </p>
      </div>
      <ol
        aria-label="Recovery codes"
        className="grid grid-cols-1 gap-x-6 gap-y-1.5 rounded-md border border-slate-200 bg-white px-4 py-3 font-mono text-sm text-slate-900 sm:grid-cols-2"
      >
        {codes.map((code) => (
          <li key={code} className="tracking-wide">
            {code}
          </li>
        ))}
      </ol>
      <div className="flex flex-wrap gap-2">
        <Button variant="secondary" size="sm" icon={<Copy aria-hidden="true" className="size-4" />} onClick={() => void onCopy()}>
          Copy codes
        </Button>
        <Button
          variant="secondary"
          size="sm"
          icon={<Download aria-hidden="true" className="size-4" />}
          onClick={() => downloadText(recoveryCodesText(codes), RECOVERY_CODES_FILENAME)}
        >
          Download as text file
        </Button>
      </div>
      <p aria-live="polite" className="text-xs text-slate-500 empty:hidden">
        {copy === "copied" ? "Copied to the clipboard." : copy === "failed" ? "Couldn't copy. Select the codes and copy them yourself." : ""}
      </p>
      <div>
        <label htmlFor={checkboxId} className="flex items-center gap-2 text-sm font-medium text-slate-700">
          <input
            id={checkboxId}
            type="checkbox"
            checked={saved}
            onChange={(e) => {
              setSaved(e.target.checked);
              if (e.target.checked) setUnsaved(false);
            }}
            aria-invalid={unsaved || undefined}
            aria-describedby={unsaved ? errorId : undefined}
            className="size-4 accent-brand-600"
          />
          I have saved these codes
        </label>
        {unsaved ? (
          <p id={errorId} className="mt-1.5 text-xs text-red-600">
            Save the codes first, then tick this box.
          </p>
        ) : null}
      </div>
      <Button type="submit" className="w-full sm:w-auto">
        {doneLabel}
      </Button>
    </form>
  );
}
