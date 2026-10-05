/**
 * Note files: the client-side checks that give immediate feedback before an upload (the
 * server stays authoritative: it recognises types by content and enforces every limit),
 * and how a file's size and type are shown.
 */
import { describeError, fieldErrors, isApiError } from "@/lib/api/errors";

export const ALLOWED_EXTENSIONS = ["pdf", "png", "jpg", "jpeg", "webp", "docx", "xlsx", "csv", "txt"] as const;
export const FILE_ACCEPT = ALLOWED_EXTENSIONS.map((ext) => `.${ext}`).join(",");
export const MAX_FILE_BYTES = 10 * 1024 * 1024;
export const MAX_FILES_PER_NOTE = 10;
export const STORAGE_DOWN = "File storage is unavailable right now. Try again in a few minutes.";

const ALLOWED = new Set<string>(ALLOWED_EXTENSIONS);
const ALLOWED_LABEL = ALLOWED_EXTENSIONS.map((ext) => ext.toUpperCase()).join(", ");

export function extensionOf(name: string): string {
  const dot = name.lastIndexOf(".");
  return dot > 0 ? name.slice(dot + 1).toLowerCase() : "";
}

/** Why this file can't be uploaded, or null when it may be tried. */
export function fileProblem(file: Pick<File, "name" | "size">): string | null {
  if (!ALLOWED.has(extensionOf(file.name))) return `This file type isn't allowed. Allowed: ${ALLOWED_LABEL}.`;
  if (file.size === 0) return "This file is empty.";
  if (file.size > MAX_FILE_BYTES) return "Files can be at most 10 MB.";
  return null;
}

export interface PickResult {
  accepted: File[];
  /** "name: reason", one per refused file. */
  refused: string[];
}

/** Sorts picked files into those to upload and those refused, with room for `room` more. */
export function pickFiles(files: readonly File[], room: number): PickResult {
  const accepted: File[] = [];
  const refused: string[] = [];
  for (const file of files) {
    const problem = fileProblem(file);
    if (problem) refused.push(`${file.name}: ${problem}`);
    else if (accepted.length >= room) refused.push(`${file.name}: A note can have at most ${MAX_FILES_PER_NOTE} files.`);
    else accepted.push(file);
  }
  return { accepted, refused };
}

const sizeFormat = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 1 });

/** "820 B", "14 KB", "1.2 MB" (binary units, as the 10 MB limit). */
export function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${sizeFormat.format(Math.round(bytes / 1024))} KB`;
  return `${sizeFormat.format(bytes / (1024 * 1024))} MB`;
}

export type FileKind = "image" | "sheet" | "text" | "other";

export function fileKind(extension: string): FileKind {
  switch (extension.toLowerCase()) {
    case "png":
    case "jpg":
    case "jpeg":
    case "webp":
      return "image";
    case "xlsx":
    case "csv":
      return "sheet";
    case "pdf":
    case "docx":
    case "txt":
      return "text";
    default:
      return "other";
  }
}

/** What a failed upload shows next to the file. */
export function uploadProblem(error: unknown): string {
  if (isApiError(error, 503, "storage_unavailable")) return STORAGE_DOWN;
  if (isApiError(error, 413)) return error.message || "Files can be at most 10 MB.";
  const file = fieldErrors(error).file?.[0];
  return file ?? describeError(error).message;
}
