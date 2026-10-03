"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";

import { hardNavigate } from "@/lib/browser";

import { authApi } from "./api";

/** Ends the server session, then reloads onto sign-in with every cache discarded. */
export function useSignOut() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: authApi.logout,
    onSuccess: () => {
      // Leave first: hardNavigate unmounts the app before the page goes (lib/browser), so
      // clearing the cache afterwards can't make a still-mounted query refetch /auth/me and
      // get a pointless 401 (seen in the Phase 2 live walkthrough).
      hardNavigate("/login?reason=signed-out", "signed-out");
      queryClient.clear();
    },
  });
}
