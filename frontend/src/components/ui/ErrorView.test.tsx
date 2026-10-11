import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ErrorView } from "./ErrorView";

describe("ErrorView (ARCH-10)", () => {
  it("says something went wrong, offers Try again and the way home", async () => {
    const retry = vi.fn();
    render(<ErrorView onRetry={retry} />);
    expect(screen.getByRole("heading", { level: 1, name: "Something went wrong" })).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("This page couldn't be shown.");
    expect(screen.getByRole("link", { name: "Go to Dashboard" })).toHaveAttribute("href", "/dashboard");
    await userEvent.setup().click(screen.getByRole("button", { name: "Try again" }));
    expect(retry).toHaveBeenCalledTimes(1);
  });

  it("shows the server error's digest as a reference", () => {
    render(<ErrorView digest="2481630012" onRetry={() => undefined} />);
    expect(screen.getByText("Reference: 2481630012")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("quote the reference below");
  });

  it("shows no reference without a digest, and never something that isn't one", () => {
    const { rerender } = render(<ErrorView onRetry={() => undefined} />);
    expect(screen.queryByText(/Reference:/)).not.toBeInTheDocument();
    rerender(<ErrorView digest={"Cannot read properties of undefined (reading 'email')"} onRetry={() => undefined} />);
    expect(screen.queryByText(/Reference:/)).not.toBeInTheDocument();
    expect(document.body).not.toHaveTextContent("reading 'email'");
  });

  it("fills the viewport on its own page, or sits inside the shell", () => {
    const { container, rerender } = render(<ErrorView onRetry={() => undefined} />);
    expect(container.firstElementChild).toHaveClass("py-24");
    rerender(<ErrorView fullPage onRetry={() => undefined} />);
    expect(container.firstElementChild).toHaveClass("min-h-dvh");
  });
});
