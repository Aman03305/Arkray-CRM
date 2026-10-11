import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { darkModules, MFA_SETUP as SETUP, QR_FIXTURE, QR_FIXTURE_DARK_MODULES, RECOVERY_CODES as CODES } from "@/test/mfa-fixtures";
import { apiError, mockApi, renderWithProviders } from "@/test/render";

import { groupSecret, MfaSetupFlow } from "./MfaSetupFlow";
import { isValidMatrix, qrPath } from "./QrCode";

async function startSetup(onComplete = vi.fn()) {
  const api = mockApi({
    "POST /api/v1/auth/mfa/setup": { status: 200, body: SETUP },
    "POST /api/v1/auth/mfa/confirm": { status: 200, body: { recovery_codes: CODES } },
  });
  renderWithProviders(<MfaSetupFlow onComplete={onComplete} />);
  const user = userEvent.setup();
  await user.type(screen.getByLabelText("Current password"), "my passphrase");
  await user.click(screen.getByRole("button", { name: "Continue" }));
  await screen.findByRole("img", { name: /QR code/ });
  return { api, user, onComplete };
}

describe("QrCode", () => {
  it("draws one rectangle per run of dark modules, inside a quiet zone", () => {
    expect(qrPath(QR_FIXTURE)).toBe("M4 4h1v1h-1zM6 4h2v1h-2zM5 5h1v1h-1zM4 6h3v1h-3z");
  });

  it("refuses a matrix that isn't square or isn't made of 0 and 1", () => {
    expect(isValidMatrix(QR_FIXTURE)).toBe(true);
    expect(isValidMatrix({ size: 2, rows: ["10", "1"] })).toBe(false);
    expect(isValidMatrix({ size: 2, rows: ["10", "12"] })).toBe(false);
    expect(isValidMatrix({ size: 3, rows: ["10", "01"] })).toBe(false);
  });
});

describe("MfaSetupFlow", () => {
  it("asks for the password, then shows the QR code as SVG and the key in groups", async () => {
    const { api } = await startSetup();
    expect(api.callsTo("POST", "/api/v1/auth/mfa/setup")[0]!.body).toEqual({ current_password: "my passphrase" });
    const qr = screen.getByRole("img", { name: /QR code/ });
    expect(qr.tagName.toLowerCase()).toBe("svg");
    expect(qr).toHaveAttribute("viewBox", "0 0 12 12"); // 4 modules plus 4 quiet ones each side
    expect(darkModules(qr)).toBe(QR_FIXTURE_DARK_MODULES);
    // Drawn, never an image URL or injected markup (the CSP refuses both).
    expect(document.querySelector("img")).toBeNull();
    expect(qr.outerHTML).not.toMatch(/data:|style=/);
    expect(screen.getByText(groupSecret(SETUP.secret))).toBeInTheDocument();
    expect(groupSecret("JBSWY3DPEHPK3PXP")).toBe("JBSW Y3DP EHPK 3PXP");
    expect(screen.getByText("Add Arkray CRM to your authenticator app")).toHaveFocus();
  });

  it("puts a wrong current password on its field", async () => {
    mockApi({
      "POST /api/v1/auth/mfa/setup": apiError(400, "validation_error", "Invalid.", { current_password: ["Your current password is incorrect."] }),
    });
    renderWithProviders(<MfaSetupFlow onComplete={vi.fn()} />);
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Current password"), "wrong");
    await user.click(screen.getByRole("button", { name: "Continue" }));
    await waitFor(() => expect(screen.getByLabelText("Current password")).toHaveAccessibleDescription("Your current password is incorrect."));
  });

  it("explains that it's already on (409)", async () => {
    mockApi({ "POST /api/v1/auth/mfa/setup": apiError(409, "mfa_already_enabled", "Two-step verification is already on.") });
    renderWithProviders(<MfaSetupFlow onComplete={vi.fn()} />);
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Current password"), "my passphrase");
    await user.click(screen.getByRole("button", { name: "Continue" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Two-step verification is already on.");
  });

  it("turns on with a first code, then shows the recovery codes once and needs them saved", async () => {
    const { api, user, onComplete } = await startSetup();
    await user.type(screen.getByLabelText("Authentication code"), "123456");
    await user.click(screen.getByRole("button", { name: "Turn on two-step verification" }));
    const list = await screen.findByRole("list", { name: "Recovery codes" });
    expect(within(list).getAllByRole("listitem").map((item) => item.textContent)).toEqual(CODES);
    expect(api.callsTo("POST", "/api/v1/auth/mfa/confirm")[0]!.body).toEqual({ code: "123456" });
    // The secret is gone from the page once it's on.
    expect(document.body.textContent).not.toContain(groupSecret(SETUP.secret));
    expect(screen.queryByRole("img", { name: /QR code/ })).not.toBeInTheDocument();

    // Not without saying they're saved.
    await user.click(screen.getByRole("button", { name: "Done" }));
    expect(onComplete).not.toHaveBeenCalled();
    const checkbox = screen.getByRole("checkbox", { name: "I have saved these codes" });
    expect(checkbox).toHaveAttribute("aria-invalid", "true");
    expect(checkbox).toHaveAccessibleDescription("Save the codes first, then tick this box.");
    expect(checkbox).toHaveFocus();

    await user.click(checkbox);
    await user.click(screen.getByRole("button", { name: "Done" }));
    expect(onComplete).toHaveBeenCalledTimes(1);
  });

  it("copies the codes, and downloads them as a text file", async () => {
    const { user } = await startSetup();
    await user.type(screen.getByLabelText("Authentication code"), "123456");
    await user.click(screen.getByRole("button", { name: "Turn on two-step verification" }));
    await screen.findByRole("list", { name: "Recovery codes" });

    await user.click(screen.getByRole("button", { name: "Copy codes" }));
    expect(await screen.findByText("Copied to the clipboard.")).toBeInTheDocument();
    expect(await navigator.clipboard.readText()).toBe(CODES.join("\n"));

    // jsdom has no object URLs: stand-ins, put back afterwards.
    const blobs: Blob[] = [];
    const original = { create: URL.createObjectURL, revoke: URL.revokeObjectURL };
    URL.createObjectURL = vi.fn((blob: Blob) => {
      blobs.push(blob);
      return "blob:arkray/1";
    });
    URL.revokeObjectURL = vi.fn();
    const clicked: HTMLAnchorElement[] = [];
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      clicked.push(this);
    });
    await user.click(screen.getByRole("button", { name: "Download as text file" }));
    expect(clicked).toHaveLength(1);
    expect(clicked[0]!.download).toBe("arkray-crm-recovery-codes.txt");
    expect(clicked[0]!.isConnected).toBe(false); // removed again
    const text = await blobs[0]!.text();
    for (const code of CODES) expect(text).toContain(code);
    await waitFor(() => expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:arkray/1"));
    click.mockRestore();
    URL.createObjectURL = original.create;
    URL.revokeObjectURL = original.revoke;
  });

  it("puts a wrong first code on the field", async () => {
    const { user } = await startSetup();
    mockApi({ "POST /api/v1/auth/mfa/confirm": apiError(400, "invalid_mfa_code", "That code isn't right. Try again.") });
    await user.type(screen.getByLabelText("Authentication code"), "000000");
    await user.click(screen.getByRole("button", { name: "Turn on two-step verification" }));
    await waitFor(() => expect(screen.getByLabelText("Authentication code")).toHaveAccessibleDescription(/That code isn't right/));
    expect(screen.getByRole("img", { name: /QR code/ })).toBeInTheDocument();
  });

  it("starts again when the pending set-up is gone", async () => {
    const { user } = await startSetup();
    mockApi({ "POST /api/v1/auth/mfa/confirm": apiError(400, "mfa_setup_required", "Start the set-up again.") });
    await user.type(screen.getByLabelText("Authentication code"), "123456");
    await user.click(screen.getByRole("button", { name: "Turn on two-step verification" }));
    expect(await screen.findByLabelText("Current password")).toHaveValue("");
    expect(screen.getByRole("status")).toHaveTextContent("Start the set-up again.");
  });
});
