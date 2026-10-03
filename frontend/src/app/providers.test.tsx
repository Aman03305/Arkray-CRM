import { act, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AUTH_CHANNEL, LEAVING_EVENT } from "@/lib/browser";

import { Providers } from "./providers";

const browser = vi.hoisted(() => ({ reloadPage: vi.fn() }));
vi.mock("@/lib/browser", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/browser")>()),
  reloadPage: browser.reloadPage,
}));

function renderApp() {
  return render(
    <Providers>
      <p>Other users&apos; data</p>
    </Providers>,
  );
}

describe("Providers: no data outlives a change of identity (review P1)", () => {
  beforeEach(() => {
    browser.reloadPage.mockReset();
    window.history.replaceState(null, "", "/admin/users");
  });

  it("renders nothing once the app starts leaving, so the back/forward cache holds no data", () => {
    renderApp();
    expect(screen.getByText("Other users' data")).toBeInTheDocument();
    act(() => {
      window.dispatchEvent(new Event(LEAVING_EVENT));
    });
    expect(screen.queryByText("Other users' data")).not.toBeInTheDocument();
  });

  it("reloads a page restored from the back/forward cache", () => {
    renderApp();
    const restored = new Event("pageshow") as PageTransitionEvent;
    Object.defineProperty(restored, "persisted", { value: true });
    window.dispatchEvent(restored);
    expect(browser.reloadPage).toHaveBeenCalledTimes(1);
  });

  it("ignores ordinary page loads", () => {
    renderApp();
    window.dispatchEvent(new Event("pageshow"));
    expect(browser.reloadPage).not.toHaveBeenCalled();
  });

  it("reloads when another tab signs in or out (review P2)", async () => {
    renderApp();
    const otherTab = new BroadcastChannel(AUTH_CHANNEL);
    otherTab.postMessage("signed-out");
    otherTab.close();
    await waitFor(() => expect(browser.reloadPage).toHaveBeenCalled());
    expect(screen.queryByText("Other users' data")).not.toBeInTheDocument();
  });

  it("doesn't reload the page it is already leaving on hearing its own announcement", async () => {
    renderApp();
    act(() => {
      window.dispatchEvent(new Event(LEAVING_EVENT)); // this tab is signing out ...
    });
    const sameTab = new BroadcastChannel(AUTH_CHANNEL); // ... and announces it
    sameTab.postMessage("signed-out");
    sameTab.close();
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(browser.reloadPage).not.toHaveBeenCalled();
  });

  it("leaves public pages alone when another tab signs in", async () => {
    window.history.replaceState(null, "", "/login");
    renderApp();
    const otherTab = new BroadcastChannel(AUTH_CHANNEL);
    otherTab.postMessage("signed-in");
    otherTab.close();
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(browser.reloadPage).not.toHaveBeenCalled();
  });
});
