import { useQuery } from "@tanstack/react-query";

import { apiFetch } from "@/lib/api/client";
import type { InvitationPreview, LoginRequest, ViewerDto } from "@/lib/api/types";
import { VIEWER_QUERY_KEY } from "@/lib/query-client";
import { toViewer, type Viewer } from "@/lib/viewer";

const AUTH = "/api/v1/auth";

export const authApi = {
  me: async (): Promise<Viewer> => toViewer(await apiFetch<ViewerDto>(`${AUTH}/me`)),
  login: async (body: LoginRequest): Promise<Viewer> =>
    toViewer(await apiFetch<ViewerDto>(`${AUTH}/login`, { method: "POST", body })),
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
