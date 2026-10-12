import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { ApiError, apiFetch } from "@/lib/api/client";
import { createQueryClient } from "@/lib/query-client";
import { apiError, mockApi } from "@/test/render";

import { StepUpProvider } from "./StepUpProvider";

const DEACTIVATE = "/api/v1/admin/users/u7/deactivate";
const MFA_REQUIRED = apiError(403, "mfa_required", "Confirm it's you first.");
const VERIFY = "POST /api/v1/auth/mfa/verify";

function renderProvider() {
  const client = createQueryClient();
  render(
    <QueryClientProvider client={client}>
      <StepUpProvider>
        <button type="button">Deactivate</button>
      </StepUpProvider>
    </QueryClientProvider>,
  );
  return client;
}

/** A route refused for want of a recent second factor until `verified()` says otherwise. */
function guarded(body: unknown, verified: () => boolean) {
  return () => (verified() ? { status: 200, body } : MFA_REQUIRED);
}

describe("step-up verification (ADR-0033)", () => {
  it("without the provider, the refusal is simply the caller's error", async () => {
    mockApi({ [`POST ${DEACTIVATE}`]: MFA_REQUIRED });
    await expect(apiFetch(DEACTIVATE, { method: "POST" })).rejects.toMatchObject({ status: 403, code: "mfa_required" });
  });

  it("asks for a code, verifies it, and repeats the refused request exactly once", async () => {
    let verified = false;
    const api = mockApi({
      [`POST ${DEACTIVATE}`]: guarded({ id: "u7", status: "deactivated" }, () => verified),
      [VERIFY]: () => {
        verified = true;
        return { status: 204 };
      },
    });
    const client = renderProvider();
    screen.getByRole("button", { name: "Deactivate" }).focus();
    const result = apiFetch(DEACTIVATE, { method: "POST" });

    const dialog = await screen.findByRole("dialog", { name: "Confirm it's you" });
    expect(dialog).toHaveAccessibleDescription(/Enter the code from your authenticator app/);
    const field = screen.getByLabelText("Authentication code");
    expect(field).toHaveFocus();
    expect(field).toHaveAttribute("autocomplete", "one-time-code");

    const user = userEvent.setup();
    await user.type(field, "654321");
    await user.click(screen.getByRole("button", { name: "Verify" }));
    await expect(result).resolves.toEqual({ id: "u7", status: "deactivated" });

    expect(api.callsTo("POST", DEACTIVATE)).toHaveLength(2);
    expect(api.callsTo("POST", "/api/v1/auth/mfa/verify")[0]!.body).toEqual({ code: "654321" });
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Deactivate" })).toHaveFocus(); // back where it was
    // The code is kept nowhere: not on the page, not in the mutation cache.
    expect(document.body.innerHTML).not.toContain("654321");
    expect(JSON.stringify(client.getMutationCache().getAll().map((m) => m.state.variables ?? null))).not.toContain("654321");
  });

  it("Escape cancels: the caller gets the original refusal and nothing is sent again", async () => {
    const api = mockApi({ [`POST ${DEACTIVATE}`]: MFA_REQUIRED });
    renderProvider();
    const result = apiFetch(DEACTIVATE, { method: "POST" });
    result.catch(() => undefined);
    await screen.findByRole("dialog", { name: "Confirm it's you" });
    await userEvent.setup().keyboard("{Escape}");
    const error = await result.catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 403, code: "mfa_required" });
    expect(api.callsTo("POST", DEACTIVATE)).toHaveLength(1);
    expect(api.callsTo("POST", "/api/v1/auth/mfa/verify")).toHaveLength(0);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("requests refused at the same time share one prompt, and each is repeated once", async () => {
    let verified = false;
    const api = mockApi({
      [`POST ${DEACTIVATE}`]: guarded({ id: "u7" }, () => verified),
      "POST /api/v1/admin/support-sessions": guarded({ id: "s1" }, () => verified),
      [VERIFY]: () => {
        verified = true;
        return { status: 204 };
      },
    });
    renderProvider();
    const first = apiFetch(DEACTIVATE, { method: "POST" });
    const second = apiFetch("/api/v1/admin/support-sessions", { method: "POST", body: { target: "u7", reason: "Help" } });
    await screen.findByRole("dialog", { name: "Confirm it's you" });
    await waitFor(() => expect(api.calls).toHaveLength(2));
    expect(screen.getAllByRole("dialog")).toHaveLength(1);

    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Authentication code"), "111222");
    await user.click(screen.getByRole("button", { name: "Verify" }));
    await expect(first).resolves.toEqual({ id: "u7" });
    await expect(second).resolves.toEqual({ id: "s1" });
    expect(api.callsTo("POST", "/api/v1/auth/mfa/verify")).toHaveLength(1);
    expect(api.callsTo("POST", DEACTIVATE)).toHaveLength(2);
    expect(api.callsTo("POST", "/api/v1/admin/support-sessions")).toHaveLength(2);
  });

  it("a second refusal after verifying is final: no loop, no second prompt", async () => {
    const api = mockApi({ [`POST ${DEACTIVATE}`]: MFA_REQUIRED, [VERIFY]: { status: 204 } });
    renderProvider();
    const result = apiFetch(DEACTIVATE, { method: "POST" });
    result.catch(() => undefined);
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText("Authentication code"), "123456");
    await user.click(screen.getByRole("button", { name: "Verify" }));
    await expect(result).rejects.toMatchObject({ code: "mfa_required" });
    expect(api.callsTo("POST", DEACTIVATE)).toHaveLength(2);
    expect(api.callsTo("POST", "/api/v1/auth/mfa/verify")).toHaveLength(1);
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("keeps the dialog open on a wrong code, and accepts a recovery code", async () => {
    let verified = false;
    const api = mockApi({
      [`POST ${DEACTIVATE}`]: guarded({ id: "u7" }, () => verified),
      [VERIFY]: (call) => {
        if ("code" in (call.body as object)) return apiError(400, "invalid_mfa_code", "That code isn't right. Try again.");
        verified = true;
        return { status: 204 };
      },
    });
    renderProvider();
    const result = apiFetch(DEACTIVATE, { method: "POST" });
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText("Authentication code"), "000000");
    await user.click(screen.getByRole("button", { name: "Verify" }));
    const field = screen.getByLabelText("Authentication code");
    await waitFor(() => expect(field).toHaveAccessibleDescription(/That code isn't right/));
    expect(field).toHaveFocus();
    expect(screen.getByRole("dialog")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Use a recovery code instead" }));
    expect(screen.getByLabelText("Recovery code")).toHaveFocus();
    await user.type(screen.getByLabelText("Recovery code"), "WXYZ-2345-6789-ABCD");
    await user.click(screen.getByRole("button", { name: "Verify" }));
    await expect(result).resolves.toEqual({ id: "u7" });
    expect(api.callsTo("POST", "/api/v1/auth/mfa/verify").map((c) => c.body)).toEqual([
      { code: "000000" },
      { recovery_code: "WXYZ-2345-6789-ABCD" },
    ]);
  });

  it("explains a second factor locked by too many wrong codes", async () => {
    mockApi({
      [`POST ${DEACTIVATE}`]: guarded({ id: "u7" }, () => false),
      [VERIFY]: apiError(403, "mfa_locked", "Locked."),
    });
    renderProvider();
    void apiFetch(DEACTIVATE, { method: "POST" }).catch(() => undefined);
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText("Authentication code"), "123456");
    await user.click(screen.getByRole("button", { name: "Verify" }));
    const field = screen.getByLabelText("Authentication code");
    await waitFor(() => expect(field).toHaveAccessibleDescription(/two-step verification is locked for your account/));
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("never asks on the sign-in and two-step endpoints themselves", async () => {
    mockApi({ "POST /api/v1/auth/mfa/recovery-codes": MFA_REQUIRED });
    renderProvider();
    await expect(apiFetch("/api/v1/auth/mfa/recovery-codes", { method: "POST", body: { code: "1" } })).rejects.toMatchObject({
      code: "mfa_required",
    });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});
