import { useQuery } from "@tanstack/react-query";

import { apiFetch } from "@/lib/api/client";
import type {
  InvitationPreview,
  LoginRequest,
  MfaChallenge,
  MfaFactorRequest,
  MfaRecoveryCodes,
  MfaSetup,
  MfaStatus,
  ViewerDto,
} from "@/lib/api/types";
import { VIEWER_QUERY_KEY } from "@/lib/query-client";
import { toViewer, type Viewer } from "@/lib/viewer";

const AUTH = "/api/v1/auth";
export const MFA_STATUS_QUERY_KEY = ["mfa-status"] as const;

/** Signed in, or the password was right and a second step is needed (ADR-0033: 202). */
export type LoginResult = { kind: "signed-in"; viewer: Viewer } | { kind: "mfa-required"; expiresIn: number };

function isChallenge(body: ViewerDto | MfaChallenge): body is MfaChallenge {
  return (body as Partial<MfaChallenge>).mfa_required === true;
}

export const authApi = {
  me: async (): Promise<Viewer> => toViewer(await apiFetch<ViewerDto>(`${AUTH}/me`)),
  login: async (body: LoginRequest): Promise<LoginResult> => {
    const answer = await apiFetch<ViewerDto | MfaChallenge>(`${AUTH}/login`, { method: "POST", body });
    return isChallenge(answer) ? { kind: "mfa-required", expiresIn: answer.expires_in } : { kind: "signed-in", viewer: toViewer(answer) };
  },
  /** The second step of a sign-in: a code from the authenticator app, or a recovery code. */
  loginMfa: async (body: MfaFactorRequest): Promise<Viewer> =>
    toViewer(await apiFetch<ViewerDto>(`${AUTH}/login/mfa`, { method: "POST", body })),
  logout: () => apiFetch<void>(`${AUTH}/logout`, { method: "POST" }),
  changePassword: (currentPassword: string, newPassword: string) =>
    apiFetch<void>(`${AUTH}/password/change`, {
      method: "POST",
      body: { current_password: currentPassword, new_password: newPassword },
    }),
  requestPasswordReset: (email: string) =>
    apiFetch<{ detail: string }>(`${AUTH}/password-reset`, { method: "POST", body: { email } }),
  confirmPasswordReset: (token: string, newPassword: string) =>
    apiFetch<void>(`${AUTH}/password-reset/confirm`, {
      method: "POST",
      body: { token, new_password: newPassword },
    }),
  verifyInvitation: (token: string) =>
    apiFetch<InvitationPreview>(`${AUTH}/invitations/verify`, { method: "POST", body: { token } }),
  acceptInvitation: (token: string, password: string) =>
    apiFetch<InvitationPreview>(`${AUTH}/invitations/accept`, {
      method: "POST",
      body: { token, password },
    }),
};

/** The signed-in user's own two-step verification (Settings, and the enforced set-up). */
export const mfaApi = {
  status: () => apiFetch<MfaStatus>(`${AUTH}/mfa`),
  /** Starts (or restarts) set-up: the secret and QR code, shown once. 409 when already on. */
  setup: (currentPassword: string) =>
    apiFetch<MfaSetup>(`${AUTH}/mfa/setup`, { method: "POST", body: { current_password: currentPassword } }),
  /** Turns it on with a first code from the app; the recovery codes come back once. */
  confirm: (code: string) => apiFetch<MfaRecoveryCodes>(`${AUTH}/mfa/confirm`, { method: "POST", body: { code } }),
  replaceRecoveryCodes: (code: string) =>
    apiFetch<MfaRecoveryCodes>(`${AUTH}/mfa/recovery-codes`, { method: "POST", body: { code } }),
  disable: (currentPassword: string, code: string) =>
    apiFetch<void>(`${AUTH}/mfa/disable`, { method: "POST", body: { current_password: currentPassword, code } }),
  /** Step-up: renews "recently verified" for this session. */
  verify: (body: MfaFactorRequest) => apiFetch<void>(`${AUTH}/mfa/verify`, { method: "POST", body }),
};

/**
 * The signed-in user. A 401 here (or anywhere) sends the browser to sign-in. Re-checked
 * whenever the tab regains focus, so a session ended elsewhere (sign-out in another tab,
 * idle timeout, deactivation) is noticed as soon as the user looks at the tab again.
 */
export function useViewerQuery() {
  return useQuery({
    queryKey: VIEWER_QUERY_KEY,
    queryFn: authApi.me,
    staleTime: 60_000,
    refetchOnWindowFocus: "always",
  });
}
