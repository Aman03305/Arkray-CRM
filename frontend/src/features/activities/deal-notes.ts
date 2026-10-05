"use client";

import { type InfiniteData, useQueryClient } from "@tanstack/react-query";
import { useCallback, useMemo, useState } from "react";

import type { Note, NotePage } from "@/lib/api/types";
import { randomUuid } from "@/lib/random";
import type { Workspace } from "@/lib/workspace";

import { activitiesApi, activityKeys, timelineKeys } from "./api";
import { uploadProblem } from "./attachments";

/**
 * A deal's cached notes: patched at once with what a write returned (so the screen never
 * shows the old text or file list in between), then reloaded with every timeline, which
 * shows notes too.
 */
export function useDealNotesCache(workspace: Workspace, opportunityId: string) {
  const queryClient = useQueryClient();
  return useMemo(() => {
    const key = activityKeys.dealNotes(workspace, opportunityId);
    return {
      patch(noteId: string, change: (note: Note) => Note) {
        queryClient.setQueryData<InfiniteData<NotePage, string | null>>(key, (data) =>
          data
            ? {
                ...data,
                pages: data.pages.map((page) => ({
                  ...page,
                  results: page.results.map((note) => (note.id === noteId ? change(note) : note)),
                })),
              }
            : data,
        );
      },
      /** After a file was added or removed: the notes, the note itself and every timeline. */
      refresh(noteId: string) {
        void queryClient.invalidateQueries({ queryKey: key });
        void queryClient.invalidateQueries({ queryKey: activityKeys.detail(workspace, noteId) });
        void queryClient.invalidateQueries({ queryKey: timelineKeys.all });
      },
    };
  }, [queryClient, workspace, opportunityId]);
}

export type UploadStatus = "waiting" | "uploading" | "done" | "failed";

export interface UploadItem {
  key: string;
  file: File;
  status: UploadStatus;
  error: string | null;
}

/**
 * Files going to one note, one at a time. Each keeps its own state; a failure shows its
 * message and can be retried (the note itself is already saved). With `dropDone`, a file
 * leaves the queue once stored (it is then in the note's file list).
 */
export function useUploadQueue(workspace: Workspace, opportunityId: string, { dropDone = false } = {}) {
  const cache = useDealNotesCache(workspace, opportunityId);
  const [items, setItems] = useState<UploadItem[]>([]);

  const uploadOne = useCallback(
    async (noteId: string, item: UploadItem): Promise<boolean> => {
      const update = (change: Partial<UploadItem>) =>
        setItems((list) => list.map((entry) => (entry.key === item.key ? { ...entry, ...change } : entry)));
      update({ status: "uploading", error: null });
      try {
        const stored = await activitiesApi.upload(workspace, noteId, item.file);
        cache.patch(noteId, (note) =>
          note.attachments.some((file) => file.id === stored.id) ? note : { ...note, attachments: [...note.attachments, stored] },
        );
        if (dropDone) setItems((list) => list.filter((entry) => entry.key !== item.key));
        else update({ status: "done" });
        return true;
      } catch (error) {
        update({ status: "failed", error: uploadProblem(error) });
        return false;
      }
    },
    [workspace, cache, dropDone],
  );

  /** Uploads `files` in order; true when every one was stored. */
  const start = useCallback(
    async (noteId: string, files: readonly File[]): Promise<boolean> => {
      const added: UploadItem[] = files.map((file) => ({ key: randomUuid(), file, status: "waiting", error: null }));
      setItems((list) => [...list, ...added]);
      let allStored = true;
      for (const item of added) {
        if (!(await uploadOne(noteId, item))) allStored = false;
      }
      cache.refresh(noteId);
      return allStored;
    },
    [uploadOne, cache],
  );

  const retry = useCallback(
    async (noteId: string, item: UploadItem): Promise<boolean> => {
      const stored = await uploadOne(noteId, item);
      cache.refresh(noteId);
      return stored;
    },
    [uploadOne, cache],
  );

  const dismiss = useCallback((key: string) => setItems((list) => list.filter((entry) => entry.key !== key)), []);

  return {
    items,
    busy: items.some((item) => item.status === "waiting" || item.status === "uploading"),
    start,
    retry,
    dismiss,
  };
}
