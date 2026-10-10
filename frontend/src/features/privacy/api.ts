/**
 * Privacy requests, for administrators (privacy.manage; refused inside a support session):
 * exporting a customer's or a staff member's data, and pseudonymising a deactivated user
 * (docs/privacy.md). An export is built by a job and downloaded, as a ZIP, only by the
 * administrator who requested it, until it expires: the list holds only their own exports.
 */
import { apiFetch } from "@/lib/api/client";
import type { DataExport, DataExportList, ExportRequest, PseudonymiseResult } from "@/lib/api/types";

const EXPORTS = "/api/v1/admin/privacy/exports";

/** The Data requests page. */
export const DATA_REQUESTS_HREF = "/admin/data-requests";

export const privacyKeys = {
  all: ["privacy"] as const,
  exports: ["privacy", "exports"] as const,
};

/** A ticket or case reference: letters, digits and . _ / # - (never a description of the person). */
export const REFERENCE_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._/#-]{0,63}$/;
export const REFERENCE_MAX = 64;

const one = (id: string, action = "") => `${EXPORTS}/${encodeURIComponent(id)}${action ? `/${action}` : ""}`;

export const privacyApi = {
  /** Your own exports, newest first. */
  exports: () => apiFetch<DataExportList>(EXPORTS),
  /** 202: queued; a job builds the ZIP. */
  requestExport: (body: ExportRequest) => apiFetch<DataExport>(EXPORTS, { method: "POST", body }),
  export: (id: string) => apiFetch<DataExport>(one(id)),
  /** Irreversible: name and email become a pseudonym, the password goes. */
  pseudonymise: (userId: string, confirmEmail: string) =>
    apiFetch<PseudonymiseResult>(`/api/v1/admin/privacy/users/${encodeURIComponent(userId)}/pseudonymise`, {
      method: "POST",
      body: { confirm_email: confirmEmail },
    }),
};

/**
 * The export's download, opened as an ordinary link: same-origin, the session cookie
 * authenticates it, and the browser saves the file (never read into the page's memory).
 */
export function exportDownloadHref(id: string): string {
  return one(id, "download");
}
