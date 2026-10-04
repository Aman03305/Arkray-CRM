/**
 * The signed-in user as the UI sees them.
 *
 * The UI checks *capabilities* (mirroring backend `identity.policy.Capability`), never role
 * names, so new roles need no frontend changes. These checks only decide what to *show*:
 * every permission is enforced again by the API. Hiding a button is not security.
 * `roleLabel` is for display only (the profile page), never for decisions.
 */
import type { ViewerDto } from "./api/types";

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
  };
}

/** Ask Arkray is shown when the CRM has it turned on and the viewer may use it. */
export function canAsk(viewer: Viewer | null | undefined): boolean {
  return viewer?.features.ask === true && hasCapability(viewer, "ai.query");
}

export function hasCapability(viewer: Viewer | null | undefined, capability: Capability): boolean {
  return viewer?.capabilities.includes(capability) ?? false;
}

export function initials(fullName: string): string {
  const parts = fullName.trim().split(/\s+/).filter(Boolean);
  const first = parts[0] ?? "";
  const last = parts.length > 1 ? (parts.at(-1) ?? "") : "";
  return (first.charAt(0) + last.charAt(0)).toUpperCase();
}
