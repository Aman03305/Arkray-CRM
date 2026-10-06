"use client";

import { CircleCheck, Download, File as FileGeneric, FileImage, FileSpreadsheet, FileText, Paperclip, RotateCw, Trash2, X } from "lucide-react";
import { useId } from "react";

import { Button } from "@/components/ui/Button";
import { Spinner } from "@/components/ui/Spinner";
import type { Attachment } from "@/lib/api/types";
import { formatDateTime, formatRelative } from "@/lib/format";
import type { Workspace } from "@/lib/workspace";

import { fileUrls } from "./api";
import { extensionOf, FILE_ACCEPT, type FileKind, fileKind, formatFileSize } from "./attachments";
import { useClock } from "./clock";
import type { UploadItem } from "./deal-notes";

const ICONS: Record<FileKind, typeof FileGeneric> = { image: FileImage, sheet: FileSpreadsheet, text: FileText, other: FileGeneric };

function FileIcon({ name, extension }: { name: string; extension?: string }) {
  const Icon = ICONS[fileKind(extension || extensionOf(name))];
  return (
    <span className="flex size-10 shrink-0 items-center justify-center rounded-md bg-slate-100 text-slate-500">
      <Icon aria-hidden="true" className="size-5" />
    </span>
  );
}

/** The name on one line, cut with an ellipsis; the whole name on hover. */
function FileName({ name }: { name: string }) {
  return (
    <p className="truncate text-sm font-medium text-slate-900" title={name}>
      {name}
    </p>
  );
}

const ROW = "flex min-w-0 items-center gap-2.5 rounded-md border border-slate-200 bg-white px-2.5 py-2";
const ICON_BUTTON =
  "inline-flex size-8 shrink-0 items-center justify-center rounded-full text-slate-500 hover:bg-slate-100 hover:text-slate-900 focus-visible:outline-2 focus-visible:outline-brand-600 disabled:cursor-not-allowed disabled:opacity-50";

export function RelativeTime({ iso }: { iso: string }) {
  const clock = useClock();
  return (
    <time dateTime={iso} title={formatDateTime(iso)}>
      {formatRelative(iso, new Date(clock))}
    </time>
  );
}

/**
 * A real file input (keyboard and screen readers reach it) shown as a pill button. The
 * value is cleared after each pick, so the same file can be picked again.
 */
export function FilePicker({
  label,
  context,
  onPick,
  disabled = false,
}: {
  label: string;
  /** Read out after the label where the picker repeats down a list ("to note: Budget…"). */
  context?: string;
  onPick: (files: File[]) => void;
  disabled?: boolean;
}) {
  const id = useId();
  return (
    <span className="relative inline-flex">
      <input
        id={id}
        type="file"
        multiple
        accept={FILE_ACCEPT}
        disabled={disabled}
        className="peer sr-only"
        onChange={(event) => {
          const files = Array.from(event.target.files ?? []);
          event.target.value = "";
          if (files.length) onPick(files);
        }}
      />
      <label
        htmlFor={id}
        className="inline-flex h-8 cursor-pointer items-center gap-1.5 rounded-full border border-slate-300 bg-white px-2.5 text-sm font-medium text-slate-700 hover:bg-slate-50 peer-focus-visible:outline-2 peer-focus-visible:outline-offset-2 peer-focus-visible:outline-brand-600 peer-disabled:cursor-not-allowed peer-disabled:text-slate-400"
      >
        <Paperclip aria-hidden="true" className="size-4" />
        {label}
        {context ? <span className="sr-only"> {context}</span> : null}
      </label>
    </span>
  );
}

/** Files refused before upload (wrong type, empty, too large, too many): one line each. */
export function RefusedFiles({ messages }: { messages: readonly string[] }) {
  if (!messages.length) return null;
  return (
    <div role="alert" className="space-y-0.5 text-xs text-red-700 [overflow-wrap:anywhere]">
      {messages.map((message, index) => (
        <p key={index}>{message}</p>
      ))}
    </div>
  );
}

/** A note's stored files. */
export function AttachmentList({
  workspace,
  files,
  onRemove,
}: {
  workspace: Workspace;
  files: readonly Attachment[];
  /** Offered when the viewer may change the note. */
  onRemove?: (file: Attachment) => void;
}) {
  if (!files.length) return null;
  return (
    <ul aria-label="Files" className="mt-2 grid min-w-0 gap-1.5 sm:grid-cols-2">
      {files.map((file) => (
        <AttachmentRow key={file.id} workspace={workspace} file={file} onRemove={onRemove} />
      ))}
    </ul>
  );
}

function AttachmentRow({ workspace, file, onRemove }: { workspace: Workspace; file: Attachment; onRemove?: (file: Attachment) => void }) {
  const scanning = file.scan_status === "pending";
  const blocked = file.scan_status === "rejected";
  const available = file.downloadable && !scanning && !blocked;
  const preview = fileUrls.preview(workspace, file.id);
  return (
    <li className={ROW}>
      {available && file.previewable ? (
        <a
          href={preview}
          target="_blank"
          rel="noopener noreferrer"
          className="size-10 shrink-0 overflow-hidden rounded-md bg-slate-100 focus-visible:outline-2 focus-visible:outline-brand-600"
        >
          {/* eslint-disable-next-line @next/next/no-img-element -- an access-checked API file, not a static asset */}
          <img src={preview} alt={file.name} loading="lazy" width={40} height={40} className="size-10 object-cover" />
        </a>
      ) : (
        <FileIcon name={file.name} extension={file.extension} />
      )}
      <div className="min-w-0 flex-1">
        <FileName name={file.name} />
        <p className="flex flex-wrap items-center gap-x-1.5 text-xs text-slate-500">
          <span>{formatFileSize(file.size)}</span>
          <span aria-hidden="true">·</span>
          <RelativeTime iso={file.created_at} />
          {scanning ? (
            <span className="text-amber-800">· Checking for viruses…</span>
          ) : blocked ? (
            <span className="font-medium text-red-700">· Blocked by virus scan</span>
          ) : available ? (
            <>
              <span aria-hidden="true">·</span>
              <a
                href={fileUrls.download(workspace, file.id)}
                download
                className="inline-flex items-center gap-1 font-medium text-brand-700 hover:underline"
              >
                <Download aria-hidden="true" className="size-3.5" />
                Download <span className="sr-only">{file.name}</span>
              </a>
            </>
          ) : null}
        </p>
      </div>
      {onRemove ? (
        <button type="button" onClick={() => onRemove(file)} aria-label={`Remove ${file.name}`} className={ICON_BUTTON}>
          <Trash2 aria-hidden="true" className="size-4" />
        </button>
      ) : null}
    </li>
  );
}

/** Files picked for a new note, before it is saved. */
export function PickedFiles({ files, onRemove, disabled }: { files: readonly File[]; onRemove: (index: number) => void; disabled?: boolean }) {
  if (!files.length) return null;
  return (
    <ul aria-label="Files to attach" className="grid min-w-0 gap-1.5">
      {files.map((file, index) => (
        <li key={`${file.name}-${file.size}-${file.lastModified}-${index}`} className={ROW}>
          <FileIcon name={file.name} />
          <div className="min-w-0 flex-1">
            <FileName name={file.name} />
            <p className="text-xs text-slate-500">{formatFileSize(file.size)}</p>
          </div>
          <button type="button" onClick={() => onRemove(index)} disabled={disabled} aria-label={`Remove ${file.name}`} className={ICON_BUTTON}>
            <X aria-hidden="true" className="size-4" />
          </button>
        </li>
      ))}
    </ul>
  );
}

/** Files on their way to a note: waiting, uploading, stored, or failed with Retry. */
export function UploadList({
  items,
  busy,
  onRetry,
  onDismiss,
}: {
  items: readonly UploadItem[];
  busy: boolean;
  onRetry: (item: UploadItem) => void;
  onDismiss?: (key: string) => void;
}) {
  const failed = items.filter((item) => item.status === "failed").length;
  const done = items.filter((item) => item.status === "done").length;
  const current = items.findIndex((item) => item.status === "uploading");
  const summary = busy
    ? `Uploading file ${Math.max(current, 0) + 1} of ${items.length}…`
    : failed
      ? `${failed} ${failed === 1 ? "file" : "files"} not uploaded.`
      : done
        ? `${done} ${done === 1 ? "file" : "files"} uploaded.`
        : "";
  return (
    <div className="space-y-1.5">
      <p aria-live="polite" className="sr-only">
        {summary}
      </p>
      {items.length ? (
        <ul aria-label="Uploads" className="grid min-w-0 gap-1.5">
          {items.map((item) => (
            <li key={item.key} className={`${ROW} flex-wrap ${item.status === "failed" ? "border-red-200 bg-red-50" : ""}`}>
              <FileIcon name={item.file.name} />
              <div className="min-w-0 flex-1">
                <FileName name={item.file.name} />
                <p className="flex items-center gap-1.5 text-xs text-slate-500">
                  {item.status === "uploading" ? (
                    <>
                      <Spinner className="size-3" /> Uploading…
                    </>
                  ) : item.status === "waiting" ? (
                    "Waiting"
                  ) : item.status === "done" ? (
                    <span className="inline-flex items-center gap-1 text-emerald-700">
                      <CircleCheck aria-hidden="true" className="size-3.5" /> Uploaded
                    </span>
                  ) : (
                    formatFileSize(item.file.size)
                  )}
                </p>
              </div>
              {item.status === "failed" ? (
                <>
                  <div className="flex shrink-0 items-center gap-1">
                    <Button
                      variant="secondary"
                      size="sm"
                      disabled={busy}
                      onClick={() => onRetry(item)}
                      icon={<RotateCw aria-hidden="true" className="size-3.5" />}
                      aria-label={`Retry ${item.file.name}`}
                    >
                      Retry
                    </Button>
                    {onDismiss ? (
                      <button type="button" onClick={() => onDismiss(item.key)} aria-label={`Dismiss ${item.file.name}`} className={ICON_BUTTON}>
                        <X aria-hidden="true" className="size-4" />
                      </button>
                    ) : null}
                  </div>
                  <p role="alert" className="basis-full text-xs text-red-700 [overflow-wrap:anywhere]">
                    {item.error}
                  </p>
                </>
              ) : null}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
