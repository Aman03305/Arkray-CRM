import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";

import { adminViewer } from "@/test/fixtures";
import { mockApi, renderWithProviders } from "@/test/render";

import { useSignOut } from "./useSignOut";

const nav = vi.hoisted(() => ({ hardNavigate: vi.fn() }));
vi.mock("@/lib/browser", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/browser")>()),
  hardNavigate: nav.hardNavigate,
}));

function SignOut() {
  const signOut = useSignOut();
  return (
    <button type="button" onClick={() => signOut.mutate()}>
      Sign out
    </button>
  );
}

it("leaves the page before clearing the cache, so nothing refetches as a signed-out user", async () => {
  mockApi({ "POST /api/v1/auth/logout": { status: 204 } });
  const { client } = renderWithProviders(<SignOut />, { viewer: adminViewer });
  const clear = vi.spyOn(client, "clear");
  await userEvent.setup().click(screen.getByRole("button", { name: "Sign out" }));
  await waitFor(() => expect(clear).toHaveBeenCalled());
  expect(nav.hardNavigate).toHaveBeenCalledWith("/login?reason=signed-out", "signed-out");
  expect(nav.hardNavigate.mock.invocationCallOrder[0]).toBeLessThan(clear.mock.invocationCallOrder[0]!);
});
