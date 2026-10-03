"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, ChevronDown } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { type FormEvent, type ReactNode, useEffect, useId, useRef, useState } from "react";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { SelectField, TextAreaField, TextField } from "@/components/ui/Field";
import { NotFoundView } from "@/components/ui/NotFoundView";
import { PageHeader } from "@/components/ui/PageHeader";
import { Skeleton } from "@/components/ui/Skeleton";
import { useFocusFirstInvalid } from "@/components/ui/useFocusFirstInvalid";
import { apiFetch } from "@/lib/api/client";
import { describeError, fieldErrors, isApiError } from "@/lib/api/errors";
import type { Lead, LeadCreateRequest, WorkspaceDto } from "@/lib/api/types";
import { setFlash } from "@/lib/flash";
import { fromBusinessDateTimeInput, toBusinessDateTimeInput } from "@/lib/format";
import { randomUuid } from "@/lib/random";
import { useViewer } from "@/lib/viewer-context";
import { leadHref, type Workspace, workspaceHref } from "@/lib/workspace";

import { leadKeys, leadsApi } from "./api";
import {
  changedFields,
  createRequest,
  type Draft,
  draftFromLead,
  EMPTY_DRAFT,
  FIELD_LABELS,
  hasIdentity,
  mergeConflict,
  type ProfileField,
  updateRequest,
} from "./draft";
import { DuplicateNotice } from "./DuplicateNotice";
import { countryName, leadPermissions, useLeadOptions, useLeadWriteSync } from "./hooks";
import { OwnerSelect } from "./OwnerSelect";

type Mode = { kind: "create" } | { kind: "edit"; leadId: string };

const ADDRESS_FIELDS: ProfileField[] = ["address_line_1", "address_line_2", "city", "state", "postal_code", "country"];
const KNOWN_FIELDS = new Set<string>([...Object.keys(FIELD_LABELS), "status", "owner"]);

function FormSection({ title, description, children, collapsible = false, open = true, onToggle }: {
  title: string;
  description?: string;
  children: ReactNode;
  collapsible?: boolean;
  open?: boolean;
  onToggle?: () => void;
}) {
  const bodyId = useId();
  return (
    <section aria-label={title} className="rounded-lg border border-slate-200 bg-white">
      <div className="flex items-start justify-between gap-4 px-5 pt-4">
        <div>
          <h2 className="text-sm font-semibold text-slate-900">
            {collapsible ? (
              <button type="button" aria-expanded={open} aria-controls={bodyId} onClick={onToggle} className="inline-flex items-center gap-1.5 hover:text-brand-700">
                <ChevronDown aria-hidden="true" className={`size-4 transition-transform ${open ? "" : "-rotate-90"}`} />
                {title}
              </button>
            ) : (
              title
            )}
          </h2>
          {description ? <p className="mt-0.5 text-xs text-slate-500">{description}</p> : null}
        </div>
      </div>
      <div id={bodyId} hidden={!open} className="grid gap-4 px-5 pb-5 pt-4 sm:grid-cols-2">
        {children}
      </div>
    </section>
  );
}

/** Create a lead, or edit one: the same form in every workspace. */
export function LeadFormView({ workspace, mode }: { workspace: Workspace; mode: Mode }) {
  const lead = useQuery({
    queryKey: leadKeys.detail(workspace, mode.kind === "edit" ? mode.leadId : ""),
    queryFn: () => leadsApi.get(workspace, mode.kind === "edit" ? mode.leadId : ""),
    enabled: mode.kind === "edit",
  });
  const viewer = useViewer();
  const permissions = leadPermissions(viewer, workspace);

  if (mode.kind === "create") {
    if (!permissions.canCreate) return <NotFoundView />;
    return <LeadForm workspace={workspace} lead={null} />;
  }
  // Data first: once the form is open, a failed background reload must never replace it
  // (and the user's unsaved edits) with an error page (Phase 2 review).
  if (lead.data) {
    if (!permissions.canWrite) return <NotFoundView />;
    if (lead.data.archived_at) {
      return (
        <Alert tone="info" title="This lead is archived" action={<Link href={leadHref(workspace, lead.data.id)} className="font-medium underline">Back to the lead</Link>}>
          Restore it before making changes.
        </Alert>
      );
    }
    // Keyed by id: a different lead starts a fresh form.
    return <LeadForm key={lead.data.id} workspace={workspace} lead={lead.data} />;
  }
  if (isApiError(lead.error, 404)) return <NotFoundView />;
  if (lead.isError) {
    const { message, requestId } = describeError(lead.error);
    return (
      <Alert
        tone="error"
        title="This lead couldn't be loaded"
        requestId={requestId}
        action={
          <Button variant="secondary" size="sm" onClick={() => void lead.refetch()} loading={lead.isFetching}>
            Try again
          </Button>
        }
      >
        {message}
      </Alert>
    );
  }
  return (
    <div aria-busy="true" className="space-y-4">
      <Skeleton className="h-7 w-56" />
      <Skeleton className="h-64 w-full" />
      <span className="sr-only">Loading lead</span>
    </div>
  );
}

function LeadForm({ workspace, lead }: { workspace: Workspace; lead: Lead | null }) {
  const router = useRouter();
  const queryClient = useQueryClient();
  const viewer = useViewer();
  const options = useLeadOptions();
  const permissions = leadPermissions(viewer, workspace);
  const editing = lead !== null;
  const form = useRef<HTMLFormElement>(null);

  const [base, setBase] = useState<Draft>(() => (lead ? draftFromLead(lead) : EMPTY_DRAFT));
  const [version, setVersion] = useState(lead?.version ?? 0);
  const [draft, setDraft] = useState<Draft>(base);
  const [status, setStatus] = useState("");
  const [owner, setOwner] = useState("");
  const [ownerLabel, setOwnerLabel] = useState("");
  const [clientErrors, setClientErrors] = useState<Record<string, string[]>>({});
  const [conflict, setConflict] = useState<{ latest: Lead } | null>(null);
  const [reloadFailed, setReloadFailed] = useState(false);
  const [reviewFields, setReviewFields] = useState<ProfileField[]>([]);
  const [openAddress, setOpenAddress] = useState(() => ADDRESS_FIELDS.some((f) => base[f]));
  const [openMore, setOpenMore] = useState(() => Boolean(base.description));
  // A create retried with exactly the same body reuses its key (so a timeout can't create
  // two leads); any difference at all (a field, the status, the owner) is a new request with
  // a new key, which the server would otherwise refuse as a reused key.
  const idempotency = useRef<{ body: string; key: string } | null>(null);
  const keyFor = (body: LeadCreateRequest): string => {
    const serialised = JSON.stringify(body);
    if (idempotency.current?.body !== serialised) idempotency.current = { body: serialised, key: randomUuid() };
    return idempotency.current.key;
  };
  const sync = useLeadWriteSync(workspace);

  const subject = useQuery({
    queryKey: ["workspace", workspace.kind === "user" ? workspace.userId : ""],
    queryFn: () => apiFetch<WorkspaceDto>(`/api/v1/workspaces/${workspace.kind === "user" ? encodeURIComponent(workspace.userId) : "me"}`),
    enabled: workspace.kind === "user",
  });

  const defaultStatus = options.data?.statuses.find((s) => s.is_default)?.key ?? "";
  const chosenStatus = status || defaultStatus;
  const dirty = changedFields(base, draft).length > 0 || (!editing && (status !== "" || owner !== ""));

  const save = useMutation({
    mutationFn: () => {
      if (lead) return leadsApi.update(workspace, lead.id, updateRequest(base, draft, version));
      const body = createRequest(draft, {
        status: chosenStatus || undefined,
        owner: permissions.choosesOwner ? owner : undefined,
      });
      return leadsApi.create(workspace, body, keyFor(body));
    },
    // Caches follow the write even if the form has been left meanwhile; navigating away is
    // done per call (onSubmit), so a save finishing later never pulls the user back here.
    onSuccess: sync,
    onError: async (error) => {
      if (!lead || !isApiError(error, 409)) return;
      setReloadFailed(false);
      try {
        // Fetched directly, not through the page's query: a failure here must not turn the
        // page (and the unsaved edits) into an error view.
        const latest = await leadsApi.get(workspace, lead.id);
        queryClient.setQueryData(leadKeys.detail(workspace, lead.id), latest);
        setConflict({ latest });
      } catch {
        setConflict(null);
        setReloadFailed(true);
      }
    },
  });

  // Keyboard users land on the way out of a conflict as soon as it is offered.
  useEffect(() => {
    if (conflict) form.current?.querySelector<HTMLElement>("[data-conflict-apply]")?.focus();
  }, [conflict]);

  // Warn before a reload or tab close would throw away unsaved changes.
  useEffect(() => {
    if (!dirty || save.isPending || save.isSuccess) return;
    const warn = (event: BeforeUnloadEvent) => event.preventDefault();
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty, save.isPending, save.isSuccess]);

  const set = (field: ProfileField) => (value: string) => setDraft((d) => ({ ...d, [field]: value }));

  const focusLater = (selector: string) =>
    requestAnimationFrame(() => form.current?.querySelector<HTMLElement>(selector)?.focus());

  const server = fieldErrors(save.error);
  const errors: Record<string, string[] | undefined> = { ...server, ...clientErrors };
  useFocusFirstInvalid(form, Object.keys(clientErrors).length ? clientErrors : save.error);

  // Open a collapsed section when the server reports a problem inside it.
  const addressOpen = openAddress || ADDRESS_FIELDS.some((f) => errors[f]?.length);
  const moreOpen = openMore || Boolean(errors.description?.length);

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    if (save.isPending) return;
    if (conflict) {
      focusLater("[data-conflict-apply]"); // resolve the conflict first
      return;
    }
    const problems: Record<string, string[]> = {};
    if (!hasIdentity(draft)) problems.first_name = ["Enter the person's name or their organization."];
    if (!editing && permissions.choosesOwner && !owner) problems.owner = ["Choose who owns this lead."];
    if (draft.last_contacted_at && !fromBusinessDateTimeInput(draft.last_contacted_at)) {
      problems.last_contacted_at = ["Enter a valid date and time."];
    }
    setClientErrors(problems);
    if (Object.keys(problems).length) return;
    if (editing && !dirty) {
      router.push(leadHref(workspace, lead.id));
      return;
    }
    setReloadFailed(false);
    save.mutate(undefined, {
      onSuccess: (saved) => {
        setFlash(lead ? "Changes saved." : "Lead created.");
        router.push(leadHref(workspace, saved.id));
      },
    });
  };

  const applyMine = () => {
    if (!conflict) return;
    const latest = draftFromLead(conflict.latest);
    const { merged, overlapping } = mergeConflict(base, draft, latest);
    setBase(latest);
    setDraft(merged);
    setVersion(conflict.latest.version);
    setReviewFields(overlapping);
    setConflict(null);
    save.reset();
    // Straight to what needs a look: the first field both people changed, or Save.
    focusLater(overlapping.length ? `[name="${overlapping[0]}"]` : 'button[type="submit"]');
  };

  const discardMine = () => {
    if (!conflict) return;
    const latest = draftFromLead(conflict.latest);
    setBase(latest);
    setDraft(latest);
    setVersion(conflict.latest.version);
    setReviewFields([]);
    setConflict(null);
    save.reset();
    focusLater('button[type="submit"]');
  };

  const banner = describeError(save.error);
  const unmapped = save.isError && !isApiError(save.error, 409) && !Object.keys(server).some((f) => KNOWN_FIELDS.has(f));
  const text = (field: ProfileField, props: Partial<Parameters<typeof TextField>[0]> = {}) => (
    <TextField
      label={FIELD_LABELS[field]}
      name={field}
      value={draft[field]}
      onChange={(e) => set(field)(e.target.value)}
      errors={errors[field]}
      optional
      autoComplete="off"
      {...props}
    />
  );
  const cancelHref = lead ? leadHref(workspace, lead.id) : workspaceHref(workspace, "leads");
  const activeSources = (options.data?.sources ?? []).filter((s) => s.is_active || s.key === base.source);
  const activeStatuses = (options.data?.statuses ?? []).filter((s) => s.is_active);

  return (
    <>
      <Link href={cancelHref} className="mb-4 inline-flex items-center gap-1 text-sm text-slate-600 hover:text-slate-900">
        <ArrowLeft aria-hidden="true" className="size-4" />
        {lead ? lead.display_name : "Leads"}
      </Link>
      <PageHeader title={lead ? "Edit lead" : "New lead"} subtitle={lead ? undefined : "Only a name or an organization is required. Add the rest when you know it."} />

      <form ref={form} onSubmit={onSubmit} noValidate className="max-w-4xl space-y-4">
        {conflict ? (
          <Alert
            tone="error"
            title="Someone else changed this lead while you were editing"
            action={
              <div className="flex flex-wrap gap-2">
                <Button size="sm" onClick={applyMine} data-conflict-apply>
                  Apply my changes to the latest version
                </Button>
                <Button size="sm" variant="secondary" onClick={discardMine}>
                  Discard my changes
                </Button>
              </div>
            }
          >
            Your changes haven&apos;t been saved yet. Nothing was overwritten.
          </Alert>
        ) : isApiError(save.error, 409) ? (
          reloadFailed ? (
            <Alert tone="error">
              Someone else changed this lead, and its latest version couldn&apos;t be loaded here. Your changes are still in the form, not saved.
              Copy anything you need, then go back to the lead and try again.
            </Alert>
          ) : (
            <Alert tone="info">Someone else changed this lead. Loading the latest version…</Alert>
          )
        ) : unmapped ? (
          <Alert tone="error" requestId={banner.requestId}>
            {server.non_field_errors?.join(" ") ?? banner.message}
          </Alert>
        ) : null}
        {reviewFields.length ? (
          <Alert tone="info" title="Review before saving">
            Your changes were applied to the latest version. These fields were also changed by someone else; your values are kept:{" "}
            {reviewFields.map((f) => FIELD_LABELS[f]).join(", ")}.
          </Alert>
        ) : null}

        <FormSection title="Basic information" description="Required: a person's name, an organization, or both.">
          {text("first_name", { maxLength: 100, optional: false, hint: "Leave either name blank if the person uses one name." })}
          {text("last_name", { maxLength: 100, optional: false })}
          {text("organization_name", { maxLength: 200, optional: false })}
          {text("job_title", { maxLength: 100 })}
        </FormSection>

        <FormSection title="Contact information">
          {text("email", { type: "email", inputMode: "email", maxLength: 254, spellCheck: false })}
          {text("phone", { type: "tel", maxLength: 32, hint: "Include the country code for numbers outside India, e.g. +44 20 7946 0958." })}
          {text("mobile", { type: "tel", maxLength: 32 })}
          {text("alternate_phone", { type: "tel", maxLength: 32 })}
          <div className="sm:col-span-2">
            <DuplicateNotice
              workspace={workspace}
              email={draft.email}
              phones={[draft.phone, draft.mobile, draft.alternate_phone]}
              excludeId={lead?.id}
            />
          </div>
        </FormSection>

        <FormSection title="Sales information">
          {editing ? null : (
            <SelectField
              label="Status"
              name="status"
              value={chosenStatus}
              onChange={(e) => setStatus(e.target.value)}
              options={activeStatuses.map((s) => ({ value: s.key, label: s.name }))}
              errors={errors.status}
            />
          )}
          <SelectField
            label="Source"
            name="source"
            optional
            value={draft.source}
            onChange={(e) => set("source")(e.target.value)}
            options={[{ value: "", label: "Not set" }, ...activeSources.map((s) => ({ value: s.key, label: s.name }))]}
            errors={errors.source}
          />
          <SelectField
            label="Rating"
            name="rating"
            optional
            value={draft.rating}
            onChange={(e) => set("rating")(e.target.value)}
            options={[{ value: "", label: "Not rated" }, ...(options.data?.ratings ?? []).map((r) => ({ value: r.key, label: r.name }))]}
            errors={errors.rating}
            hint="Your judgement of their interest."
          />
          {text("last_contacted_at", {
            type: "datetime-local",
            max: toBusinessDateTimeInput(new Date().toISOString()),
            hint: "India Standard Time.",
          })}
          {editing ? null : permissions.choosesOwner ? (
            <div className="sm:col-span-2">
              <OwnerSelect
                label="Owner"
                placeholder="Choose an owner"
                value={owner}
                valueLabel={ownerLabel}
                onChange={(id, label) => {
                  setOwner(id);
                  setOwnerLabel(label);
                }}
                errors={errors.owner}
                hint="The salesperson responsible for this lead."
              />
            </div>
          ) : (
            <p className="text-sm text-slate-600 sm:col-span-2">
              Owner:{" "}
              <strong className="font-medium text-slate-900">
                {workspace.kind === "user" ? (subject.data?.subject?.full_name ?? "this user") : "you"}
              </strong>
              {errors.owner?.length ? (
                // No owner field to attach this to here: announce it instead.
                <span role="alert" className="block text-xs text-red-600">
                  {errors.owner.join(" ")}
                </span>
              ) : null}
            </p>
          )}
        </FormSection>

        <FormSection title="Address" collapsible open={addressOpen} onToggle={() => setOpenAddress((v) => !v)}>
          <div className="sm:col-span-2">{text("address_line_1", { maxLength: 200, autoComplete: "off" })}</div>
          <div className="sm:col-span-2">{text("address_line_2", { maxLength: 200 })}</div>
          {text("city", { maxLength: 100 })}
          {text("state", { maxLength: 100 })}
          {text("postal_code", { maxLength: 20 })}
          <SelectField
            label="Country"
            name="country"
            optional
            value={draft.country}
            onChange={(e) => set("country")(e.target.value)}
            options={[
              { value: "", label: "Not set" },
              ...(options.data?.countries ?? [])
                .map((code) => ({ value: code, label: countryName(code) }))
                .sort((a, b) => a.label.localeCompare(b.label)),
            ]}
            errors={errors.country}
          />
        </FormSection>

        <FormSection title="Additional information" collapsible open={moreOpen} onToggle={() => setOpenMore((v) => !v)}>
          <TextAreaField
            className="sm:col-span-2"
            label={FIELD_LABELS.description}
            name="description"
            optional
            rows={5}
            maxLength={5000}
            value={draft.description}
            onChange={(e) => set("description")(e.target.value)}
            errors={errors.description}
            hint="Background, needs, context. Up to 5,000 characters."
          />
        </FormSection>

        <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
          <Link
            href={cancelHref}
            className="inline-flex h-9 items-center justify-center rounded-md border border-slate-300 bg-white px-3.5 text-sm font-medium text-slate-700 hover:bg-slate-50"
          >
            Cancel
          </Link>
          <Button type="submit" loading={save.isPending}>
            {lead ? "Save changes" : "Create lead"}
          </Button>
        </div>
      </form>
    </>
  );
}
