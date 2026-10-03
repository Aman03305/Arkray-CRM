import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { WorkspaceBanner } from "./WorkspaceBanner";

describe("WorkspaceBanner", () => {
  it("states whose CRM is being viewed and links back to Users", () => {
    render(<WorkspaceBanner subjectName="Rahul Sharma" />);
    const banner = screen.getByRole("region", { name: "Workspace context" });
    expect(banner).toHaveTextContent("Viewing CRM for: Rahul Sharma");
    expect(screen.getByRole("link", { name: "Back to Users" })).toHaveAttribute("href", "/admin/users");
  });

  it("shows a loading placeholder until the user's name is known", () => {
    render(<WorkspaceBanner />);
    expect(screen.getByText("Loading user name")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Workspace context" })).toHaveTextContent("Viewing CRM for:");
  });
});
