/**
 * The App Router error boundaries (ARCH-10): app/(app)/error.tsx inside the signed-in shell,
 * app/error.tsx for everything else below the root layout, app/global-error.tsx for the
 * root layout itself. None of them ever shows the error's message or stack.
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import AppError from "./(app)/error";
import RootError from "./error";
import GlobalError from "./global-error";

const SECRET = "duplicate key value violates unique constraint: email=rahul@example.test";

function failure(digest?: string): Error & { digest?: string } {
  const error = new Error(SECRET) as Error & { digest?: string };
  if (digest) error.digest = digest;
  return error;
}

describe.each([
  ["app/(app)/error", AppError],
  ["app/error", RootError],
])("%s", (_, Boundary) => {
  it("offers Try again, which retries the failed part", async () => {
    const retry = vi.fn();
    render(<Boundary error={failure()} retry={retry} />);
    expect(screen.getByRole("heading", { name: "Something went wrong" })).toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole("button", { name: "Try again" }));
    expect(retry).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("link", { name: "Go to Dashboard" })).toHaveAttribute("href", "/dashboard");
  });

  it("never shows the error's message or stack, only the digest", () => {
    render(<Boundary error={failure("1234567890")} retry={() => undefined} />);
    expect(document.body).not.toHaveTextContent(/duplicate key|rahul@example\.test|at .*\.tsx/);
    expect(screen.getByText("Reference: 1234567890")).toBeInTheDocument();
  });
});

it("the shell's boundary adds no main landmark (the shell has one); the root one does", () => {
  const inShell = render(<AppError error={failure()} retry={() => undefined} />);
  expect(inShell.container.querySelector("main")).toBeNull();
  inShell.unmount();
  const root = render(<RootError error={failure()} retry={() => undefined} />);
  expect(root.container.querySelector("main")).not.toBeNull();
});

describe("app/global-error", () => {
  // Server-rendered as its own document: rendered to markup, as Next.js sends it.
  const html = renderToStaticMarkup(<GlobalError error={failure("998877")} retry={() => undefined} />);

  it("brings its own document", () => {
    expect(html).toMatch(/^<html lang="en">/);
    expect(html).toContain("<title>Something went wrong · Arkray CRM</title>");
    expect(html).toContain("<body><main>");
    expect(html).toContain("Something went wrong");
    expect(html).toContain("Try again");
    expect(html).toContain("Reference: 998877");
  });

  it("never shows the error's message", () => {
    expect(html).not.toContain("duplicate key");
    expect(html).not.toContain("rahul@example.test");
  });

  it("has no inline style attributes (the Content-Security-Policy refuses them)", () => {
    expect(html).not.toMatch(/\sstyle=/);
    for (const Boundary of [AppError, RootError]) {
      expect(renderToStaticMarkup(<Boundary error={failure("1")} retry={() => undefined} />)).not.toMatch(/\sstyle=/);
    }
  });
});
