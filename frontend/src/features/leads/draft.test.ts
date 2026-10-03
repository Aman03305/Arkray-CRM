import { describe, expect, it } from "vitest";

import { makeLead } from "@/test/fixtures";

import { cursorOf, listPath, NO_FILTERS } from "./api";
import { changedFields, createRequest, draftFromLead, EMPTY_DRAFT, hasIdentity, mergeConflict, updateRequest } from "./draft";
import { leadPermissions } from "./hooks";
import { adminViewer, RAHUL_ID, salesViewer } from "@/test/fixtures";

describe("lead drafts", () => {
  it("creates with only the fields that were filled in", () => {
    const body = createRequest(
      { ...EMPTY_DRAFT, first_name: "  Asha ", email: "asha@apollo.example", rating: "hot", last_contacted_at: "2026-09-30T10:00" },
      { status: "contacted" },
    );
    expect(body).toEqual({
      first_name: "Asha",
      email: "asha@apollo.example",
      rating: "hot",
      last_contacted_at: "2026-09-30T04:30:00.000Z", // 10:00 IST
      status: "contacted",
    });
  });

  it("edits with only what changed, and the version it started from", () => {
    const base = draftFromLead(makeLead());
    const edited = { ...base, city: "Pune", rating: "", source: "", job_title: "Lab Director " };
    expect(updateRequest(base, edited, 3)).toEqual({ version: 3, city: "Pune", rating: null, source: null });
  });

  it("clearing the last contact sends null", () => {
    const base = draftFromLead(makeLead({ last_contacted_at: "2026-09-01T06:30:00Z" }));
    expect(base.last_contacted_at).toBe("2026-09-01T12:00");
    expect(updateRequest(base, { ...base, last_contacted_at: "" }, 1)).toEqual({ version: 1, last_contacted_at: null });
  });

  it("ignores whitespace-only differences", () => {
    const base = draftFromLead(makeLead());
    expect(changedFields(base, { ...base, organization_name: " Apollo  Diagnostics " })).toEqual([]);
  });

  it("merges my edits onto the latest version and reports overlaps", () => {
    const base = draftFromLead(makeLead());
    const mine = { ...base, city: "Pune", job_title: "Head of Lab" };
    const latest = { ...base, city: "Nashik", email: "new@apollo.example" };
    const { merged, overlapping } = mergeConflict(base, mine, latest);
    expect(merged).toEqual({ ...latest, city: "Pune", job_title: "Head of Lab" });
    expect(overlapping).toEqual(["city"]);
  });

  it("does not report an overlap when both made the same change", () => {
    const base = draftFromLead(makeLead());
    expect(mergeConflict(base, { ...base, city: "Pune" }, { ...base, city: "Pune" }).overlapping).toEqual([]);
  });

  it("needs a person or an organisation", () => {
    expect(hasIdentity(EMPTY_DRAFT)).toBe(false);
    expect(hasIdentity({ ...EMPTY_DRAFT, organization_name: "Apollo" })).toBe(true);
    expect(hasIdentity({ ...EMPTY_DRAFT, last_name: " " })).toBe(false);
  });
});

describe("list requests", () => {
  it("sends only set filters; owner only organisation-wide; search needs 2 characters", () => {
    const filters = { ...NO_FILTERS, q: "a", status: "new", owner: RAHUL_ID };
    expect(listPath({ kind: "self" }, filters, null)).toBe("/api/v1/workspaces/me/leads?status=new&page_size=25");
    expect(listPath({ kind: "organization" }, { ...filters, q: "as" }, "c1")).toBe(
      `/api/v1/workspaces/all/leads?q=as&status=new&owner=${RAHUL_ID}&cursor=c1&page_size=25`,
    );
    expect(listPath({ kind: "user", userId: RAHUL_ID }, { ...NO_FILTERS, archived: true, ordering: "name" }, null)).toBe(
      `/api/v1/workspaces/${RAHUL_ID}/leads?archived=true&ordering=name&page_size=25`,
    );
  });

  it("takes only the cursor from pagination links, whatever their host", () => {
    expect(cursorOf("http://evil.example/api/v1/workspaces/me/leads?cursor=abc%3Adef&page_size=25")).toBe("abc:def");
    expect(cursorOf(null)).toBeNull();
    expect(cursorOf("::not a url")).toBeNull();
  });
});

describe("what the UI offers", () => {
  it("a sales user works in their own workspace only and never assigns", () => {
    expect(leadPermissions(salesViewer, { kind: "self" })).toEqual({
      canWrite: true,
      canAssign: false,
      canCreate: true,
      choosesOwner: false,
    });
  });

  it("an admin manages and assigns everywhere and picks owners organisation-wide", () => {
    expect(leadPermissions(adminViewer, { kind: "organization" })).toEqual({
      canWrite: true,
      canAssign: true,
      canCreate: true,
      choosesOwner: true,
    });
    expect(leadPermissions(adminViewer, { kind: "user", userId: RAHUL_ID }).choosesOwner).toBe(false);
  });

  it("viewing rights alone offer nothing to change", () => {
    const viewerOnly = { ...adminViewer, capabilities: ["crm.access_own", "crm.view_all", "workspace.view_any"] as const };
    expect(leadPermissions(viewerOnly, { kind: "user", userId: RAHUL_ID })).toEqual({
      canWrite: false,
      canAssign: false,
      canCreate: false,
      choosesOwner: false,
    });
  });
});
