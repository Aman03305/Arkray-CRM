import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useRef, useState } from "react";
import { describe, expect, it } from "vitest";

import { useModalFocus } from "./useModalFocus";

function Inner({ open }: { open: boolean }) {
  const ref = useRef<HTMLDivElement>(null);
  useModalFocus(open, ref, () => {});
  return open ? (
    <div ref={ref} role="alertdialog" aria-label="Inner">
      <button>Discard</button>
    </div>
  ) : null;
}

/** A drawer; its confirmation ("Discard your changes?") is rendered beside it, after it in the tree, as in the opportunity panel. */
function Outer({ open }: { open: boolean }) {
  const ref = useRef<HTMLDivElement>(null);
  useModalFocus(open, ref, () => {});
  return open ? (
    <div ref={ref} role="dialog" aria-label="Outer">
      <button>Cancel</button>
    </div>
  ) : null;
}

function Page() {
  const [open, setOpen] = useState(false);
  const [confirm, setConfirm] = useState(false);
  return (
    <main>
      <h1>Page</h1>
      <button onClick={() => (setOpen(true), setConfirm(false))}>Open</button>
      <Outer open={open} />
      <Inner open={confirm} />
      {open ? <button onClick={() => setConfirm(true)}>Ask</button> : null}
      {confirm ? (
        <button
          onClick={() => {
            setConfirm(false);
            setOpen(false);
          }}
        >
          Discard all
        </button>
      ) : null}
    </main>
  );
}

/** A dialog whose action opens a second one over it (a step-up code prompt over "New user"). */
function Stacked() {
  const outerRef = useRef<HTMLDivElement>(null);
  const innerRef = useRef<HTMLDivElement>(null);
  const [outer, setOuter] = useState(false);
  const [inner, setInner] = useState(false);
  useModalFocus(outer, outerRef, () => setOuter(false));
  useModalFocus(inner, innerRef, () => setInner(false));
  return (
    <main>
      <h1>Users</h1>
      <button onClick={() => setOuter(true)}>New user</button>
      {outer ? (
        <div ref={outerRef} role="dialog" aria-label="New user" tabIndex={-1}>
          <input aria-label="Email" />
          <button onClick={() => setInner(true)}>Create user</button>
        </div>
      ) : null}
      {inner ? (
        <div ref={innerRef} role="dialog" aria-label="Confirm it's you" tabIndex={-1}>
          <input aria-label="Authentication code" data-autofocus />
          <button>Verify</button>
        </div>
      ) : null}
    </main>
  );
}

describe("useModalFocus", () => {
  it("final hardening: a dialog opened over another takes focus (the outer one doesn't pull it back)", async () => {
    render(<Stacked />);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "New user" }));
    await user.click(screen.getByRole("button", { name: "Create user" }));
    expect(screen.getByRole("textbox", { name: "Authentication code" })).toHaveFocus();
    await user.keyboard("123456");
    expect(screen.getByRole("textbox", { name: "Authentication code" })).toHaveValue("123456");
    await user.keyboard("{Escape}"); // closes only the top one; focus goes back to its opener
    expect(screen.queryByRole("dialog", { name: "Confirm it's you" })).not.toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "New user" })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "Create user" })).toHaveFocus());
  });

  it("final audit UI-2: a confirmation closing together with its drawer leaves focus on the drawer's opener, not the page heading", async () => {
    render(<Page />);
    const user = userEvent.setup();
    const opener = screen.getByRole("button", { name: "Open" });
    await user.click(opener);
    await user.click(screen.getByRole("button", { name: "Ask" }));
    // Both modals are open; the outer panel's hook and the inner confirmation's hook unmount in one commit.
    await user.click(screen.getByRole("button", { name: "Discard all" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await waitFor(() => expect(opener).toHaveFocus());
  });
});
