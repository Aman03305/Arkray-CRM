import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { WorkspaceBanner } from "./WorkspaceBanner";

const RAHUL = { id: "3f2b8c1e-9a4d-4e2f-8b7a-1c2d3e4f5a6b", full_name: "Rahul Sharma", status: "active" as const };
const ANITA = { id: "8c11b60d-58f6-4eff-b571-8647cde4717e", fullName: "Anita Rao" };
const BACK = { href: "/admin/users", label: "Back to Users" };

describe("WorkspaceBanner", () => {
  it("states whose CRM is being viewed, their status, and links back to Users", () => {
    render(<WorkspaceBanner subject={RAHUL} actor={ANITA} back={BACK} />);
    const banner = screen.getByRole("region", { name: "Workspace context" });
    expect(banner).toHaveTextContent("Viewing CRM for: Rahul Sharma");
    expect(banner).toHaveTextContent("Status: Active");
    expect(within(banner).getByRole("link", { name: "Back to Users" })).toHaveAttribute("href", "/admin/users");
  });

  it("separates the signed-in administrator (the actor) from the user whose records these are", () => {
    render(<WorkspaceBanner subject={RAHUL} actor={ANITA} back={BACK} />);
    const banner = screen.getByRole("region", { name: "Workspace context" });
    expect(banner).toHaveTextContent("Signed in as Anita Rao. Changes you make here are recorded as yours.");
    expect(banner).not.toHaveTextContent("Signed in as Rahul");
  });

  it("marks the administrator's own workspace as theirs", () => {
    render(<WorkspaceBanner subject={{ ...RAHUL, id: ANITA.id, full_name: "Anita Rao" }} actor={ANITA} back={BACK} />);
    const banner = screen.getByRole("region", { name: "Workspace context" });
    expect(banner).toHaveTextContent("Viewing CRM for: Anita Rao (you)");
    expect(banner).not.toHaveTextContent("Signed in as");
  });

  it.each([
    ["deactivated", "Deactivated", "Their records are kept for reference; new records can't be added for them."],
    ["invited", "Invited", "They haven't activated their account yet, so new records can't be added for them."],
  ] as const)("explains what a %s user's workspace allows", (status, label, note) => {
    render(<WorkspaceBanner subject={{ ...RAHUL, status }} actor={ANITA} back={BACK} />);
    const banner = screen.getByRole("region", { name: "Workspace context" });
    expect(banner).toHaveTextContent(`Status: ${label}`);
    expect(banner).toHaveTextContent(note);
  });

  it("shows a placeholder, never a name, until the user is known", () => {
    render(<WorkspaceBanner actor={ANITA} back={BACK} />);
    const banner = screen.getByRole("region", { name: "Workspace context" });
    expect(screen.getByText("Loading user name")).toBeInTheDocument();
    expect(banner).toHaveTextContent("Viewing CRM for:");
    expect(banner).not.toHaveTextContent("Status:");
  });

  it("is a labelled region with no heading of its own (the page keeps its single h1)", () => {
    render(<WorkspaceBanner subject={RAHUL} actor={ANITA} back={BACK} />);
    expect(within(screen.getByRole("region", { name: "Workspace context" })).queryByRole("heading")).toBeNull();
  });
});
