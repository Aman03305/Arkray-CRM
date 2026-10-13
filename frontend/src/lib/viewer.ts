/**
 * The signed-in user as the UI sees them.
 *
 * The UI checks *capabilities* (mirroring backend `identity.policy.Capability`), never role
 * names, so new roles need no frontend changes. These checks only decide what to *show*:
 * every permission is enforced again by the API. Hiding a button is not security.
 * `roleLabel` is for display only (the profile page), never for decisions.
 */
import type { SupportSessionDto, ViewerDto } from "./api/types";

export const CAPABILITIES = [
  "crm.access_own",
  "crm.view_all",
  "workspace.view_any",
  "crm.assign_any",
  "crm.manage_any",
  "users.manage",
  "config.manage",
  "audit.view",
  "ai.query",
  "support.access",
  "privacy.manage",
  // A team administrator's counterparts (ADR-0035): the same powers over themselves and the
  // users they manage only. Distinct strings, so a check written for the organisation-wide
  // capability shows a team administrator nothing (fail closed).
  "crm.view_team",
  "workspace.view_team",
  "crm.assign_team",
  "crm.manage_team",
  "users.manage_team",
  "support.access_team",
] as const;

export type Capability = (typeof CAPABILITIES)[number];

export interface Viewer {
  id: string;
  email: string;
  firstName: string;
  lastName: string;
  fullName: string;
  roleLabel: string;
  capabilities: readonly Capability[];
  /** Deployment-wide features (configuration, not permissions): Ask Arkray turned on. */
  features: { ask: boolean };
  /** An administrator chose the password: only changing it (or signing out) works now. */
  passwordChangeRequired: boolean;
  /** Two-step verification is set up (ADR-0033). */
  mfaEnabled: boolean;
  /** Required for this role but not set up yet: only setting it up (or signing out) works now. */
  mfaEnrollmentRequired: boolean;
  /** An administrator's live support session in one user's CRM (never the user's login). */
  supportSession: SupportSession | null;
}

export interface SupportSession {
  id: string;
  target: { id: string; fullName: string };
  reason: string;
  startedAt: string;
  expiresAt: string;
}

function toSupportSession(dto: SupportSessionDto | null | undefined): SupportSession | null {
  if (!dto) return null;
  return {
    id: dto.id,
    target: { id: dto.target.id.toLowerCase(), fullName: dto.target.full_name },
    reason: dto.reason,
    startedAt: dto.started_at,
    expiresAt: dto.expires_at,
  };
}

function isCapability(value: string): value is Capability {
  return (CAPABILITIES as readonly string[]).includes(value);
}

export function toViewer(dto: ViewerDto): Viewer {
  return {
    id: dto.id,
    email: dto.email,
    firstName: dto.first_name,
    lastName: dto.last_name,
    fullName: dto.full_name,
    roleLabel: dto.role_label,
    capabilities: dto.capabilities.filter(isCapability), // unknown strings grant nothing
    // A response without the field (an older backend) offers no optional features.
    features: { ask: (dto.features as ViewerDto["features"] | undefined)?.ask === true },
    passwordChangeRequired: dto.password_change_required === true,
    mfaEnabled: dto.mfa_enabled === true,
    mfaEnrollmentRequired: dto.mfa_enrollment_required === true,
    supportSession: toSupportSession(dto.support_session),
  };
}

/** Ask Arkray is shown when the CRM has it turned on and the viewer may use it. */
export function canAsk(viewer: Viewer | null | undefined): boolean {
  return viewer?.features.ask === true && hasCapability(viewer, "ai.query");
}

export function hasCapability(viewer: Viewer | null | undefined, capability: Capability): boolean {
  return viewer?.capabilities.includes(capability) ?? false;
}

export function hasAnyCapability(viewer: Viewer | null | undefined, capabilities: readonly Capability[]): boolean {
  return capabilities.some((capability) => hasCapability(viewer, capability));
}

// --- Super administrators and team administrators (ADR-0035) ---------------------------------
// Each power exists organisation-wide (a super administrator's) and for a team (a team
// administrator's: themselves and the users they manage). These name the pair, so call sites
// read as what they offer; the API applies the reach.

/** The Users page: everyone (users.manage), or oneself and one's team (users.manage_team). */
export function canManageUsers(viewer: Viewer | null | undefined): boolean {
  return hasAnyCapability(viewer, ["users.manage", "users.manage_team"]);
}

/** Roles and teams, and administrators' accounts: the super administrator only. */
export function canManageAdmins(viewer: Viewer | null | undefined): boolean {
  return hasCapability(viewer, "users.manage");
}

/** Open another user's CRM workspace (anyone's, or a team member's). */
export function canOpenUserWorkspaces(viewer: Viewer | null | undefined): boolean {
  return hasAnyCapability(viewer, ["workspace.view_any", "workspace.view_team"]);
}

/** Choose who owns a record (the owner pickers, GET /assignees). */
export function canAssign(viewer: Viewer | null | undefined): boolean {
  return hasAnyCapability(viewer, ["crm.assign_any", "crm.assign_team"]);
}

/** Write in someone else's workspace or the overview, and correct a closed deal. */
export function canManageOthersRecords(viewer: Viewer | null | undefined): boolean {
  return hasAnyCapability(viewer, ["crm.manage_any", "crm.manage_team"]);
}

/** Start a support session in a user's CRM. */
export function canStartSupport(viewer: Viewer | null | undefined): boolean {
  return hasAnyCapability(viewer, ["support.access", "support.access_team"]);
}

/**
 * What the viewer's "all" workspace covers: the whole organisation (crm.view_all), their team
 * (crm.view_team), or nothing (null: their own workspace is their home).
 */
export function overviewKind(viewer: Viewer | null | undefined): "organization" | "team" | null {
  if (hasCapability(viewer, "crm.view_all")) return "organization";
  if (hasCapability(viewer, "crm.view_team")) return "team";
  return null;
}

export function initials(fullName: string): string {
  const parts = fullName.trim().split(/\s+/).filter(Boolean);
  const first = parts[0] ?? "";
  const last = parts.length > 1 ? (parts.at(-1) ?? "") : "";
  return (first.charAt(0) + last.charAt(0)).toUpperCase();
}
