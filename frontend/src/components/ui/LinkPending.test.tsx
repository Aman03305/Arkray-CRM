import { render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { LinkPending } from "./LinkPending";

const status = vi.hoisted(() => ({ pending: false }));
vi.mock("next/link", () => ({ useLinkStatus: () => status }));

describe("LinkPending", () => {
  it("is marked pending while its link's page is on its way, and is never announced", () => {
    status.pending = false;
    const { container, rerender } = render(<LinkPending className="bottom-1" />);
    const hint = container.firstElementChild!;
    expect(hint).toHaveAttribute("aria-hidden", "true");
    expect(hint).toHaveClass("link-pending", "absolute", "bottom-1");
    expect(hint).not.toHaveClass("is-pending");

    status.pending = true;
    rerender(<LinkPending className="bottom-1" />);
    expect(container.firstElementChild).toHaveClass("is-pending");
  });
});
