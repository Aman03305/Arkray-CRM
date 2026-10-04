/**
 * Ask Arkray API client and queries: /api/v1/workspaces/{me|all|userId}/ask...
 *
 * Every cache key starts with ["ask", workspace segment]: a question asked in Rahul's
 * workspace, and its answer arriving after a switch to Priya's, only ever fills Rahul's
 * entries, which Priya's screen doesn't observe (and the Ask view remounts per workspace).
 * Answers are kept in memory only; a sign-out reloads the page, dropping them.
 *
 * A question the router answers comes back answered at once. Otherwise it is pending and
 * polled (about every second) until answered or failed. Polling always ends: the server
 * fails a question no worker answered in time (90 s), and the browser gives up on its own
 * after POLL_LIMIT_MS from when *it* first saw the question (never comparing its clock with
 * the server's), or at once when the question is gone (404) or no longer readable (403).
 * Either way the question then reads as failed ("lost"), so the page never waits forever.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { ApiError, apiFetch } from "@/lib/api/client";
import type { AskConversation, AskConversationSummary, AskQuestion, AskStatus } from "@/lib/api/types";
import { type Workspace, workspaceApiPath, workspaceApiSegment } from "@/lib/workspace";

const POLL_MS = 1_000;
// Longer than the server's own deadline for a question (90 s): it normally fails it first.
export const POLL_LIMIT_MS = 120_000;

export const askKeys = {
  all: ["ask"] as const,
  workspace: (workspace: Workspace) => ["ask", workspaceApiSegment(workspace)] as const,
  status: (workspace: Workspace) => ["ask", workspaceApiSegment(workspace), "status"] as const,
  conversations: (workspace: Workspace) => ["ask", workspaceApiSegment(workspace), "conversations"] as const,
  conversation: (workspace: Workspace, id: string) => ["ask", workspaceApiSegment(workspace), "conversation", id] as const,
  question: (workspace: Workspace, id: string) => ["ask", workspaceApiSegment(workspace), "question", id] as const,
};

export const askApi = {
  status: (workspace: Workspace, signal?: AbortSignal) => apiFetch<AskStatus>(workspaceApiPath(workspace, "ask"), { signal }),
  ask: (workspace: Workspace, question: string, conversationId: string | null) =>
    apiFetch<AskQuestion>(workspaceApiPath(workspace, "ask"), {
      method: "POST",
      body: conversationId ? { question, conversation_id: conversationId } : { question },
    }),
  question: (workspace: Workspace, id: string, signal?: AbortSignal) =>
    apiFetch<AskQuestion>(workspaceApiPath(workspace, `ask/questions/${encodeURIComponent(id)}`), { signal }),
  conversations: (workspace: Workspace, signal?: AbortSignal) =>
    apiFetch<AskConversationSummary[]>(workspaceApiPath(workspace, "ask/conversations"), { signal }),
  conversation: (workspace: Workspace, id: string, signal?: AbortSignal) =>
    apiFetch<AskConversation>(workspaceApiPath(workspace, `ask/conversations/${encodeURIComponent(id)}`), { signal }),
  forget: (workspace: Workspace, id: string) =>
    apiFetch<void>(workspaceApiPath(workspace, `ask/conversations/${encodeURIComponent(id)}`), { method: "DELETE" }),
};

export function useAskStatus(workspace: Workspace) {
  return useQuery({
    queryKey: askKeys.status(workspace),
    queryFn: ({ signal }) => askApi.status(workspace, signal),
    staleTime: 30_000,
  });
}

export function useConversations(workspace: Workspace) {
  return useQuery({
    queryKey: askKeys.conversations(workspace),
    queryFn: ({ signal }) => askApi.conversations(workspace, signal),
    staleTime: 15_000,
  });
}

/** The question as failed because the browser lost track of it (gave up, or it is gone). */
export function lost(question: AskQuestion): AskQuestion {
  return { ...question, status: "failed", error: "lost", answer: null };
}

/** One question, polled while pending (see the module header for how polling ends). */
export function useQuestion(workspace: Workspace, initial: AskQuestion) {
  const client = useQueryClient();
  // When this browser first saw the question: the poll limit is measured from here.
  const [seenAt] = useState(() => Date.now());
  const key = askKeys.question(workspace, initial.id);
  return useQuery({
    queryKey: key,
    queryFn: async ({ signal }) => {
      const current = client.getQueryData<AskQuestion>(key) ?? initial;
      if (Date.now() - seenAt > POLL_LIMIT_MS) return lost(current);
      try {
        return await askApi.question(workspace, initial.id, signal);
      } catch (error) {
        if (error instanceof ApiError && (error.status === 404 || error.status === 403)) return lost(current);
        throw error;
      }
    },
    initialData: initial,
    // initialData is "fresh" for staleTime: a pending one is fetched again right away.
    staleTime: initial.status === "pending" ? 0 : Infinity,
    refetchInterval: (query) => (query.state.data?.status === "pending" ? POLL_MS : false),
    refetchIntervalInBackground: true,
  });
}

/** Ask in `workspace`. The result is cached under that workspace only (see the header). */
export function useAsk(workspace: Workspace) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ question, conversationId }: { question: string; conversationId: string | null }) =>
      askApi.ask(workspace, question, conversationId),
    onSuccess: (asked) => {
      client.setQueryData(askKeys.question(workspace, asked.id), asked);
      // The conversation has a new question: a cached copy of it is out of date.
      client.removeQueries({ queryKey: askKeys.conversation(workspace, asked.conversation_id) });
      void client.invalidateQueries({ queryKey: askKeys.conversations(workspace) });
    },
  });
}

export function useForgetConversation(workspace: Workspace) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (id: string) => {
      try {
        await askApi.forget(workspace, id);
      } catch (error) {
        // Already gone (forgotten in another tab): that is what was wanted.
        if (!(error instanceof ApiError && error.status === 404)) throw error;
      }
    },
    onSuccess: (_, id) => {
      client.removeQueries({ queryKey: askKeys.conversation(workspace, id) });
      void client.invalidateQueries({ queryKey: askKeys.conversations(workspace) });
    },
  });
}
