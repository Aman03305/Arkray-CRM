import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { renderWithProviders as render } from "@/test/render";

import { AppShell } from "./AppShell";

vi.mock("next/navigation", () => ({ usePathname: () => "/dashboard" }));

describe("AppShell", () => {
  it("renders page content in the main landmark with a skip link", () => {
    render(<AppShell><p>Page body</p></AppShell>);
    expect(screen.getByRole("main")).toHaveTextContent("Page body");
    expect(screen.getByRole("link", { name: "Skip to content" })).toHaveAttribute("href", "#main");
  });

  it("opens and closes the mobile navigation", async () => {
    const user = userEvent.setup();
    render(<AppShell><p>Page body</p></AppShell>);
    expect(screen.queryByRole("dialog", { name: "Navigation" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Open navigation" }));
    expect(screen.getByRole("dialog", { name: "Navigation" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Close navigation" }));
    expect(screen.queryByRole("dialog", { name: "Navigation" })).not.toBeInTheDocument();
  });

  it("closes the mobile navigation with Escape", async () => {
    const user = userEvent.setup();
    render(<AppShell><p>Page body</p></AppShell>);
    await user.click(screen.getByRole("button", { name: "Open navigation" }));
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "Navigation" })).not.toBeInTheDocument();
  });
});
