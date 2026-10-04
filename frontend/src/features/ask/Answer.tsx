"use client";

/**
 * One Ask Arkray answer, rendered from typed data (docs/rag-architecture.md).
 *
 * Safe by construction: every piece of text, from the model or from CRM records, becomes a
 * React text node. There is no HTML, no markdown parser and no URL from the answer: a link
 * is built here only from a `ref` the server resolved against this question's tool
 * results, and always points into the workspace on screen (Rahul's answer links stay in
 * Rahul's workspace).
 */
import { Quote } from "lucide-react";
import Link from "next/link";
import type { ReactNode } from "react";

import { Badge } from "@/components/ui/Badge";
import type { AskAnswer, AskAnswerBlock, AskAnswerPart, AskRecordKind, AskSource } from "@/lib/api/types";
import { activityHref, leadHref, opportunityHref, type Workspace } from "@/lib/workspace";

const KIND_LABELS: Record<AskRecordKind, string> = {
  lead: "Lead",
  opportunity: "Opportunity",
  task: "Task",
  meeting: "Meeting",
  note: "Note",
};

export function recordHref(workspace: Workspace, kind: AskRecordKind, id: string): string {
  switch (kind) {
    case "lead":
      return leadHref(workspace, id);
    case "opportunity":
      return opportunityHref(workspace, id);
    default:
      return activityHref(workspace, id);
  }
}

function RecordLink({ workspace, source }: { workspace: Workspace; source: AskSource }) {
  return (
    <Link
      href={recordHref(workspace, source.kind, source.id)}
      className="font-medium text-brand-700 underline decoration-brand-200 underline-offset-2 [overflow-wrap:anywhere] hover:decoration-brand-600"
    >
      {source.label}
    </Link>
  );
}

function Parts({ parts, sources, workspace }: { parts: readonly AskAnswerPart[]; sources: Map<string, AskSource>; workspace: Workspace }) {
  return (
    <>
      {parts.map((part, index) => {
        if (part.ref) {
          const source = sources.get(part.ref);
          return source ? <RecordLink key={index} workspace={workspace} source={source} /> : null;
        }
        const text = part.text ?? "";
        return part.bold ? <strong key={index}>{text}</strong> : <span key={index}>{text}</span>;
      })}
    </>
  );
}

/** Paragraphs, with consecutive bullets grouped into one list. */
function Blocks({ blocks, sources, workspace }: { blocks: readonly AskAnswerBlock[]; sources: Map<string, AskSource>; workspace: Workspace }) {
  const out: ReactNode[] = [];
  let bullets: AskAnswerBlock[] = [];
  const flush = () => {
    if (bullets.length) {
      const items = bullets;
      out.push(
        <ul key={`list-${out.length}`} className="ml-5 list-disc space-y-1">
          {items.map((block, index) => (
            <li key={index}>
              <Parts parts={block.parts} sources={sources} workspace={workspace} />
            </li>
          ))}
        </ul>,
      );
      bullets = [];
    }
  };
  blocks.forEach((block, index) => {
    if (block.type === "bullet") {
      bullets.push(block);
      return;
    }
    flush();
    out.push(
      <p key={`p-${index}`}>
        <Parts parts={block.parts} sources={sources} workspace={workspace} />
      </p>,
    );
  });
  flush();
  // Long unbroken text (a pasted link, a long name) wraps instead of widening the page.
  return <div className="min-w-0 space-y-2 text-sm leading-6 text-slate-800 [overflow-wrap:anywhere]">{out}</div>;
}

const MODES = {
  router: { label: "From your CRM", tone: "green", hint: "Figures straight from the CRM, no AI involved." },
  llm: { label: "AI summary", tone: "blue", hint: "Written by the AI from CRM records; figures checked against them." },
  retrieval: { label: "Matching records", tone: "neutral", hint: "The records that best match your question; no AI summary." },
} as const;

export function AnswerView({ answer, workspace }: { answer: AskAnswer; workspace: Workspace }) {
  const sources = new Map(answer.sources.map((source) => [source.ref, source]));
  const mode = MODES[answer.provenance.mode];
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={mode.tone}>{mode.label}</Badge>
        <span className="text-xs text-slate-500">{mode.hint}</span>
      </div>

      {answer.blocks.length > 0 ? <Blocks blocks={answer.blocks} sources={sources} workspace={workspace} /> : null}

      {answer.facts.length > 0 ? (
        <dl className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
          {answer.facts.map((fact) => (
            <div key={fact.label} className="min-w-0 rounded-md border border-slate-200 bg-slate-50 px-3 py-2">
              <dt className="text-xs text-slate-500 [overflow-wrap:anywhere]">{fact.label}</dt>
              <dd className="text-base font-semibold tabular-nums text-slate-900 [overflow-wrap:anywhere]">{fact.value}</dd>
            </div>
          ))}
        </dl>
      ) : null}

      {answer.citations.length > 0 ? (
        <div>
          <h3 className="mb-2 text-xs font-medium uppercase tracking-wide text-slate-500">From your notes</h3>
          <ul className="space-y-2">
            {answer.citations.map((citation, index) => {
              const source = sources.get(citation.ref);
              return (
                // One record can be quoted twice (two passages): the index keeps keys unique.
                <li key={`${citation.ref}-${index}`} className="min-w-0 rounded-md border border-slate-200 bg-white px-3 py-2 text-sm">
                  <div className="flex min-w-0 flex-wrap items-center gap-x-2 text-xs text-slate-500 [overflow-wrap:anywhere]">
                    <Quote aria-hidden="true" className="size-3.5" />
                    <span>{KIND_LABELS[citation.kind]}</span>
                    {source ? <RecordLink workspace={workspace} source={source} /> : <span>{citation.label}</span>}
                    {citation.when ? <span>· {citation.when}</span> : null}
                  </div>
                  <blockquote className="mt-1 whitespace-pre-line break-words text-slate-700 [overflow-wrap:anywhere]">{citation.snippet}</blockquote>
                </li>
              );
            })}
          </ul>
        </div>
      ) : null}

      {answer.sources.length > 0 && answer.citations.length === 0 ? (
        <div>
          <h3 className="mb-1 text-xs font-medium uppercase tracking-wide text-slate-500">Sources</h3>
          <ul className="flex flex-wrap gap-x-4 gap-y-1 text-sm">
            {answer.sources.map((source) => (
              <li key={source.ref} className="min-w-0 break-words">
                <span className="text-slate-500">{KIND_LABELS[source.kind]}: </span>
                <RecordLink workspace={workspace} source={source} />
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  );
}
