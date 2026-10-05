"use client";

/**
 * Ask Arkray in one workspace (docs/rag-architecture.md).
 *
 * Rendered inside a key per workspace segment (features/workspace/views.tsx): moving from
 * Rahul's workspace to Priya's starts a fresh view, so no question, answer, draft or
 * conversation of Rahul's is ever shown under Priya's. Requests started before the switch
 * may still finish; their results land in Rahul's cache entries (features/ask/api.ts) and
 * their handlers belong to the unmounted view, so they can't change what is on screen.
 *
 * Inside one workspace the thread changes on four actions (ask, open a conversation, start a
 * new one, forget one). Each takes a new "generation"; a result that arrives after a newer
 * action is ignored, so two conversations can never mix (an answer to A never lands in B's
 * thread, a slower open never replaces a faster one, forgetting A never clears B).
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { History, MessageSquareText, Plus, Send, Trash2 } from "lucide-react";
import { type FormEvent, type KeyboardEvent, useId, useRef, useState } from "react";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { PageHeader } from "@/components/ui/PageHeader";
import { Spinner } from "@/components/ui/Spinner";
import { selectedUserId, useWorkspaceSubject } from "@/features/workspace/api";
import { ApiError } from "@/lib/api/client";
import type { AskQuestion } from "@/lib/api/types";
import type { Workspace } from "@/lib/workspace";

import { AnswerView } from "./Answer";
import { askApi, askKeys, useAsk, useAskStatus, useConversations, useForgetConversation, useQuestion } from "./api";

const MAX_LENGTH = 1000;
const SUGGESTIONS = [
  "What is the pipeline value?",
  "How many overdue tasks are there?",
  "What meetings are there today?",
  "What concerns were raised recently?",
];

const FAILURES: Record<string, string> = {
  ai_unavailable:
    "Ask Arkray couldn't take this question right now. Pipeline, customer, task and meeting questions (like the suggestions) still work.",
  timeout: "This question took too long to answer. Please try again.",
  not_permitted: "You no longer have access to this workspace's records.",
  ai_disabled: "Ask Arkray has been turned off for this CRM.",
  lost: "The answer to this question didn't arrive. Please ask again.",
};

function whose(workspace: Workspace, subjectName: string | undefined): string {
  if (workspace.kind === "self") return "your records";
  if (workspace.kind === "organization") return "all users' records";
  return subjectName ? `${subjectName}'s records` : "the selected user's records";
}

function Turn({ workspace, initial }: { workspace: Workspace; initial: AskQuestion }) {
  const question = useQuestion(workspace, initial).data ?? initial;
  return (
    <li className="space-y-3">
      <div className="ml-auto w-fit max-w-[85%] whitespace-pre-line break-words rounded-lg bg-brand-50 px-3.5 py-2 text-sm text-slate-900 [overflow-wrap:anywhere]">
        <span className="sr-only">You asked: </span>
        {question.question}
      </div>
      <div className="min-w-0 rounded-lg border border-slate-200 bg-white p-4">
        {question.status === "pending" ? (
          <p role="status" className="flex items-center gap-2 text-sm text-slate-600">
            <Spinner /> Looking through the records…
          </p>
        ) : question.status === "failed" ? (
          <Alert tone="error">{FAILURES[question.error ?? ""] ?? "Ask Arkray couldn't answer this question."}</Alert>
        ) : question.answer ? (
          <AnswerView answer={question.answer} workspace={workspace} />
        ) : null}
      </div>
    </li>
  );
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.code === "rate_limited") return "You're asking quickly, or a question is still being answered. Please wait a moment.";
    if (error.code === "ai_busy") return "Ask Arkray is busy right now. Please try again in a minute.";
    if (error.code === "ai_disabled") return "Ask Arkray is turned off for this CRM.";
    if (error.code === "validation_error") {
      const details = error.details as Record<string, unknown> | null;
      const first = details && Object.values(details).flat().find((value) => typeof value === "string");
      if (typeof first === "string") return first;
    }
    return error.message;
  }
  return "Something went wrong. Please try again.";
}

export function AskView({ workspace }: { workspace: Workspace }) {
  const status = useAskStatus(workspace);
  const subject = useWorkspaceSubject(selectedUserId(workspace));
  const conversations = useConversations(workspace);
  const ask = useAsk(workspace);
  const forget = useForgetConversation(workspace);
  const client = useQueryClient();
  const [draft, setDraft] = useState("");
  const [conversationId, setConversationState] = useState<string | null>(null);
  const currentConversation = useRef<string | null>(null);
  const [turns, setTurns] = useState<AskQuestion[]>([]);
  const [notice, setNotice] = useState<{ tone: "error" | "info"; text: string } | null>(null);
  const [opening, setOpening] = useState<string | null>(null);
  const [confirmForget, setConfirmForget] = useState(false);
  const generation = useRef(0);
  const input = useRef<HTMLTextAreaElement>(null);
  const inputId = useId();
  const scope = whose(workspace, subject.data?.full_name);
  const latest = turns.at(-1);
  // Observes (never fetches) the newest question: its Turn polls it; this only re-renders.
  const latestState = useQuery({
    queryKey: askKeys.question(workspace, latest?.id ?? ""),
    queryFn: ({ signal }) => askApi.question(workspace, latest!.id, signal),
    enabled: false,
  });
  const waiting = ask.isPending || latestState.data?.status === "pending";

  const next = () => {
    generation.current += 1;
    return generation.current;
  };

  const setConversationId = (id: string | null) => {
    currentConversation.current = id;
    setConversationState(id);
  };

  const focusInput = () => requestAnimationFrame(() => input.current?.focus());

  const submit = (text: string) => {
    const question = text.trim();
    if (!question || waiting) return;
    const mine = next();
    const asked = conversationId;
    setNotice(null);
    ask.mutate(
      { question, conversationId: asked },
      {
        onSuccess: (result) => {
          if (generation.current !== mine) return; // another action came after: not this thread
          setTurns((current) => [...current, result]);
          setConversationId(result.conversation_id);
          setDraft((current) => (current.trim() === question ? "" : current));
        },
        onError: (error) => {
          if (generation.current !== mine) return;
          if (asked && error instanceof ApiError && error.status === 404) {
            // The conversation was forgotten elsewhere: start a new one next time.
            setConversationId(null);
            setTurns([]);
            void client.invalidateQueries({ queryKey: askKeys.conversations(workspace) });
            setNotice({ tone: "info", text: "That conversation no longer exists. Ask again to start a new one." });
            ask.reset();
          }
        },
      },
    );
  };

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    submit(draft);
  };

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    // keyCode 229: the Enter that ends an IME composition (Safari reports it this way).
    if (event.key !== "Enter" || event.shiftKey || event.nativeEvent.isComposing || event.keyCode === 229) return;
    event.preventDefault();
    submit(draft);
  };

  const startNew = () => {
    next();
    setTurns([]);
    setConversationId(null);
    setNotice(null);
    setOpening(null);
    ask.reset();
    focusInput();
  };

  const open = async (id: string) => {
    const mine = next();
    setNotice(null);
    setOpening(id);
    try {
      const conversation = await client.fetchQuery({
        queryKey: askKeys.conversation(workspace, id),
        queryFn: ({ signal }) => askApi.conversation(workspace, id, signal),
        staleTime: 0,
      });
      if (generation.current !== mine) return;
      for (const question of conversation.questions) {
        const key = askKeys.question(workspace, question.id);
        const cached = client.getQueryData<AskQuestion>(key);
        // Never replace a newer copy (a question answered meanwhile) with this one.
        if (!cached || cached.status === "pending") client.setQueryData(key, question);
      }
      setTurns([...conversation.questions]);
      setConversationId(conversation.id);
      ask.reset();
    } catch (error) {
      if (generation.current !== mine) return;
      setNotice({ tone: "error", text: errorMessage(error) });
    } finally {
      if (generation.current === mine) setOpening(null);
    }
  };

  const forgetCurrent = () => {
    const forgotten = conversationId;
    if (!forgotten) return;
    forget.mutate(forgotten, {
      onSuccess: () => {
        setConfirmForget(false);
        // Only if it is still the conversation on screen (another may have been opened).
        if (currentConversation.current === forgotten) startNew();
      },
    });
  };

  if (status.data && !status.data.enabled) {
    return (
      <>
        <PageHeader title="Ask Arkray" />
        <Alert>Ask Arkray is turned off for this CRM.</Alert>
      </>
    );
  }

  return (
    <div className="flex min-w-0 flex-col gap-6 lg:flex-row">
      <section className="min-w-0 flex-1" aria-labelledby={`${inputId}-title`}>
        <PageHeader
          title="Ask Arkray"
          subtitle={<span id={`${inputId}-scope`}>Answers come only from {scope}.</span>}
          actions={
            turns.length > 0 ? (
              <>
                <Button variant="secondary" size="sm" icon={<Plus aria-hidden="true" className="size-4" />} onClick={startNew}>
                  New conversation
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  icon={<Trash2 aria-hidden="true" className="size-4" />}
                  onClick={() => {
                    forget.reset();
                    setConfirmForget(true);
                  }}
                  disabled={!conversationId}
                >
                  Forget
                </Button>
              </>
            ) : null
          }
        />
        <h2 id={`${inputId}-title`} className="sr-only">
          Conversation
        </h2>

        {status.data?.summaries === "none" ? (
          <div className="mb-4">
            <Alert>Ask Arkray runs without an AI model here: it answers pipeline, customer, task and meeting questions, and otherwise shows the records that best match.</Alert>
          </div>
        ) : status.data?.summaries === "unavailable" ? (
          <div className="mb-4">
            <Alert>AI summaries are temporarily unavailable; answers show the best-matching records instead.</Alert>
          </div>
        ) : null}

        {notice ? (
          <div className="mb-4">
            <Alert tone={notice.tone}>{notice.text}</Alert>
          </div>
        ) : null}

        {opening ? (
          <p role="status" className="mb-4 flex items-center gap-2 text-sm text-slate-600">
            <Spinner /> Opening the conversation…
          </p>
        ) : null}

        {turns.length === 0 ? (
          <div className="mb-6 rounded-lg border border-dashed border-slate-300 bg-white px-6 py-10 text-center">
            <span className="mx-auto mb-3 flex size-10 items-center justify-center rounded-full bg-slate-100">
              <MessageSquareText aria-hidden="true" className="size-5 text-slate-500" />
            </span>
            <p className="text-sm font-medium text-slate-900">Ask about {scope}</p>
            <p className="mt-1 text-sm text-slate-500">Figures come straight from the CRM. Answers link to the records they use.</p>
            <ul className="mt-4 flex flex-wrap justify-center gap-2" aria-label="Suggested questions">
              {SUGGESTIONS.map((suggestion) => (
                <li key={suggestion}>
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={() => {
                      submit(suggestion);
                      focusInput();
                    }}
                    disabled={waiting}
                  >
                    {suggestion}
                  </Button>
                </li>
              ))}
            </ul>
          </div>
        ) : (
          <ol aria-label="Questions and answers" aria-live="polite" className="mb-6 space-y-6">
            {turns.map((turn) => (
              <Turn key={turn.id} workspace={workspace} initial={turn} />
            ))}
          </ol>
        )}

        {ask.isError && !notice ? (
          <div className="mb-3">
            <Alert tone="error" requestId={ask.error instanceof ApiError ? ask.error.requestId : null}>
              {errorMessage(ask.error)}
            </Alert>
          </div>
        ) : null}

        <form onSubmit={onSubmit} className="rounded-lg border border-slate-200 bg-white p-3 shadow-sm">
          <label htmlFor={inputId} className="sr-only">
            Ask a question about {scope}
          </label>
          <textarea
            id={inputId}
            ref={input}
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={onKeyDown}
            maxLength={MAX_LENGTH}
            rows={2}
            placeholder="Ask a question…"
            aria-describedby={`${inputId}-hint`}
            className="block w-full resize-y rounded-md border-0 bg-transparent px-1 py-1 text-sm text-slate-900 placeholder:text-slate-400 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-600"
          />
          <div className="mt-2 flex items-center justify-between gap-3">
            <p id={`${inputId}-hint`} className="text-xs text-slate-500">
              {waiting ? "Waiting for the answer… " : "Enter to ask, Shift+Enter for a new line. "}
              {draft.length > MAX_LENGTH - 100 ? `${MAX_LENGTH - draft.length} characters left.` : ""}
            </p>
            <Button type="submit" size="sm" loading={ask.isPending} disabled={!draft.trim() || waiting} icon={<Send aria-hidden="true" className="size-4" />}>
              Ask
            </Button>
          </div>
        </form>
      </section>

      <aside className="w-full min-w-0 shrink-0 lg:w-64" aria-labelledby={`${inputId}-recent`}>
        <h2 id={`${inputId}-recent`} className="mb-2 flex items-center gap-2 text-xs font-medium uppercase tracking-wide text-slate-500">
          <History aria-hidden="true" className="size-3.5" /> Recent conversations
        </h2>
        {conversations.isPending ? (
          <p className="text-sm text-slate-500">Loading…</p>
        ) : conversations.isError ? (
          <div className="space-y-2">
            <p className="text-sm text-slate-500">Couldn&apos;t load conversations.</p>
            <Button variant="secondary" size="sm" onClick={() => void conversations.refetch()}>
              Retry
            </Button>
          </div>
        ) : conversations.data.length === 0 ? (
          <p className="text-sm text-slate-500">None yet in this workspace.</p>
        ) : (
          <ul className="space-y-1">
            {conversations.data.map((conversation) => (
              <li key={conversation.id}>
                <button
                  type="button"
                  onClick={() => void open(conversation.id)}
                  aria-current={conversation.id === conversationId ? "true" : undefined}
                  aria-busy={opening === conversation.id || undefined}
                  className={`w-full truncate rounded-md px-2 py-1.5 text-left text-sm hover:bg-slate-100 ${
                    conversation.id === conversationId ? "bg-slate-100 font-medium text-slate-900" : "text-slate-700"
                  }`}
                >
                  {conversation.title || "Untitled"}
                </button>
              </li>
            ))}
          </ul>
        )}
      </aside>

      <ConfirmDialog
        open={confirmForget}
        title="Forget this conversation?"
        confirmLabel="Forget"
        tone="danger"
        busy={forget.isPending}
        error={forget.isError ? { message: errorMessage(forget.error), requestId: forget.error instanceof ApiError ? forget.error.requestId : null } : null}
        onConfirm={forgetCurrent}
        onCancel={() => setConfirmForget(false)}
      >
        Its questions and answers are deleted for good. Your CRM records are not affected.
      </ConfirmDialog>
    </div>
  );
}
