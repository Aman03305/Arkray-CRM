import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { Dialog } from "./Dialog";
import { Drawer } from "./Drawer";

function Harness({ busy = false, onClose = vi.fn() }: { busy?: boolean; onClose?: () => void }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        Open
      </button>
      <Dialog
        open={open}
        title="Edit user"
        description="Change their details."
        busy={busy}
        onClose={() => {
          onClose();
          setOpen(false);
        }}
      >
        <input aria-label="First field" />
        <button type="button">Last button</button>
      </Dialog>
    </>
  );
}

describe("Dialog", () => {
  it("is a labelled modal that takes focus and gives it back", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByRole("button", { name: "Open" }));
    const dialog = screen.getByRole("dialog", { name: "Edit user" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(dialog).toHaveAccessibleDescription("Change their details.");
    expect(screen.getByRole("button", { name: "Close dialog" })).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Open" })).toHaveFocus();
  });

  it("keeps Tab inside the dialog", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByRole("button", { name: "Open" }));
    screen.getByRole("button", { name: "Last button" }).focus();
    await user.tab();
    expect(screen.getByRole("button", { name: "Close dialog" })).toHaveFocus();
    await user.tab({ shift: true });
    expect(screen.getByRole("button", { name: "Last button" })).toHaveFocus();
  });

  it("cannot be dismissed while an action is running", async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();
    render(<Harness busy onClose={onClose} />);
    await user.click(screen.getByRole("button", { name: "Open" }));
    await user.keyboard("{Escape}");
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Close dialog" })).toBeDisabled();
  });

  it("its close button, and a drawer's, is 32 px to hit (a 16 px icon + 2 x 8 px) in the room of the old 28 px", () => {
    const modals = [
      ["Close dialog", <Dialog key="d" open title="Edit user" onClose={vi.fn()}>Body</Dialog>],
      ["Close panel", <Drawer key="p" open title="New opportunity" onClose={vi.fn()}>Body</Drawer>],
    ] as const;
    for (const [name, modal] of modals) {
      const view = render(modal);
      const close = screen.getByRole("button", { name });
      expect(close).toHaveClass("p-2", "-my-0.5", "-mr-1.5");
      expect(close.querySelector("svg")).toHaveClass("size-4");
      view.unmount();
    }
  });
});
