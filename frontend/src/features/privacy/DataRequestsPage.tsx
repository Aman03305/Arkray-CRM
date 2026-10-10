"use client";

import { useQuery } from "@tanstack/react-query";
import { Download, FileArchive } from "lucide-react";

import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { EmptyState } from "@/components/ui/EmptyState";
import { PageHeader } from "@/components/ui/PageHeader";
import { Skeleton } from "@/components/ui/Skeleton";
import { formatFileSize } from "@/features/activities/attachments";
import { describeError, isApiError } from "@/lib/api/errors";
import type { DataExport, DataExportStatus } from "@/lib/api/types";
import { formatDateTime } from "@/lib/format";

import { exportDownloadHref, privacyApi, privacyKeys } from "./api";

/** How often the list is read again while an export is being prepared. */
export const POLL_MS = 5_000;

const STATUS: Record<DataExportStatus, { label: string; tone: "neutral" | "green" | "amber" | "red" }> = {
  queued: { label: "Preparing", tone: "amber" },
  ready: { label: "Ready", tone: "green" },
  failed: { label: "Failed", tone: "red" },
  expired: { label: "Expired", tone: "neutral" },
};

const SUBJECTS = { lead: "Customer", user: "User" } as const;

const COLUMNS = ["Reference", "Subject", "Status", "Requested", "Expires", "Size", "Files", "Download"] as const;

function Time({ value }: { value: string | null }) {
  return value ? <time dateTime={value}>{formatDateTime(value)}</time> : <span className="text-slate-400">—</span>;
}

function Row({ item }: { item: DataExport }) {
  const status = STATUS[item.status];
  return (
    <tr>
      <td className="whitespace-nowrap px-4 py-3 font-medium text-slate-900">{item.reference}</td>
      <td className="whitespace-nowrap px-4 py-3 text-slate-700">{SUBJECTS[item.subject_type]}</td>
      <td className="whitespace-nowrap px-4 py-3">
        <Badge tone={status.tone}>{status.label}</Badge>
        {item.status === "failed" && item.error_code ? <span className="ml-2 text-xs text-slate-500">{item.error_code}</span> : null}
      </td>
      <td className="whitespace-nowrap px-4 py-3 text-slate-700">
        <Time value={item.created_at} />
      </td>
      <td className="whitespace-nowrap px-4 py-3 text-slate-700">
        <Time value={item.expires_at} />
      </td>
      <td className="whitespace-nowrap px-4 py-3 text-slate-700">{item.size === null ? <span className="text-slate-400">—</span> : formatFileSize(item.size)}</td>
      <td className="whitespace-nowrap px-4 py-3 text-slate-700">
        {item.status === "ready" || item.status === "expired" ? (
          <>
            {item.files_included} included
            {item.files_omitted ? <span className="block text-xs text-amber-800">{item.files_omitted} omitted</span> : null}
          </>
        ) : (
          <span className="text-slate-400">—</span>
        )}
      </td>
      <td className="whitespace-nowrap px-4 py-3 text-right">
        {item.status === "ready" ? (
          // An ordinary link: same-origin, the session cookie authenticates it, and the
          // browser saves the ZIP itself (it is never read into this page).
          <a
            href={exportDownloadHref(item.id)}
            download
            className="inline-flex h-8 items-center gap-1.5 rounded-full border border-slate-300 bg-white px-2.5 text-sm font-medium text-slate-700 hover:bg-slate-50"
          >
            <Download aria-hidden="true" className="size-4" />
            {"Download "}
            <span className="sr-only">{item.reference}</span>
          </a>
        ) : null}
      </td>
    </tr>
  );
}

/**
 * The data exports you requested (an administrator's, privacy.manage), newest first: their
 * reference, whose data (a customer or a user; never named here), where each stands, and the
 * ZIP to download once it's ready, until it expires. Only the administrator who requested an
 * export can download it. While one is being prepared the list is read again every few
 * seconds.
 */
export function DataRequestsPage() {
  const exports = useQuery({
    queryKey: privacyKeys.exports,
    queryFn: privacyApi.exports,
    refetchInterval: (query) => (query.state.data?.results.some((item) => item.status === "queued") ? POLL_MS : false),
  });
  const rows = exports.data?.results;
  const error = exports.isError && !rows ? describeError(exports.error) : null;

  return (
    <>
      <PageHeader
        title="Data requests"
        subtitle="Exports of a customer's or a user's data that you requested. Start one from the customer's page or the user's details."
      />
      {error ? (
        <Alert
          tone="error"
          title={isApiError(exports.error, 403) ? "You can't manage data requests" : "Data requests couldn't be loaded"}
          requestId={error.requestId}
          action={
            isApiError(exports.error, 403) ? null : (
              <Button variant="secondary" size="sm" onClick={() => void exports.refetch()} loading={exports.isFetching}>
                Try again
              </Button>
            )
          }
        >
          {error.message}
        </Alert>
      ) : rows && rows.length === 0 ? (
        <EmptyState icon={FileArchive} title="No data requests yet" description="Exports you request appear here, ready to download." />
      ) : (
        <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
          <table className="min-w-full divide-y divide-slate-200 text-sm" aria-busy={!rows || undefined}>
            <caption className="sr-only">Your data exports{rows ? "" : " (loading)"}</caption>
            <thead className="bg-slate-50">
              <tr>
                {COLUMNS.map((column) => (
                  <th
                    key={column}
                    scope="col"
                    className={`whitespace-nowrap px-4 py-2.5 text-xs font-medium uppercase tracking-wide text-slate-500 ${column === "Download" ? "text-right" : "text-left"}`}
                  >
                    {column === "Download" ? <span className="sr-only">{column}</span> : column}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {rows
                ? rows.map((item) => <Row key={item.id} item={item} />)
                : Array.from({ length: 3 }, (_, i) => (
                    <tr key={i}>
                      {COLUMNS.map((column) => (
                        <td key={column} className="px-4 py-3">
                          <Skeleton className="h-4 w-20" />
                        </td>
                      ))}
                    </tr>
                  ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
