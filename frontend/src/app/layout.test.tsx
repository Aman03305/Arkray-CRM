// @vitest-environment node
/**
 * E2E-1: a session that ended before a full page load (reload, a link opened later) must
 * still be reported as ended. The root layout tells the client whether the page was
 * requested with a session cookie; the cookie's value never leaves the server.
 */
import { isValidElement, type ReactElement, type ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const jar = vi.hoisted(() => ({ names: new Set<string>() }));
vi.mock("next/headers", () => ({
  cookies: async () => ({ has: (name: string) => jar.names.has(name) }),
}));
vi.mock("next/server", () => ({ connection: async () => undefined }));

import RootLayout from "./layout";
import { Providers } from "./providers";

function providersProps(tree: ReactNode): Record<string, unknown> {
  const queue: ReactNode[] = [tree];
  while (queue.length) {
    const node = queue.shift();
    if (!isValidElement(node)) continue;
    const element = node as ReactElement<Record<string, unknown> & { children?: ReactNode }>;
    if (element.type === Providers) return element.props;
    const children = element.props.children;
    queue.push(...(Array.isArray(children) ? children : [children]));
  }
  throw new Error("Providers not rendered");
}

describe("root layout: was there a session cookie? (E2E-1)", () => {
  beforeEach(() => jar.names.clear());

  it.each(["arkray_session", "__Host-arkray_session"])("yes, with %s", async (name) => {
    jar.names.add(name);
    const props = providersProps(await RootLayout({ children: <p>page</p> }));
    expect(props.sessionAtLoad).toBe(true);
  });

  it("no, without one (other cookies don't count)", async () => {
    jar.names.add("arkray_csrftoken");
    const props = providersProps(await RootLayout({ children: <p>page</p> }));
    expect(props.sessionAtLoad).toBe(false);
  });

  it("passes only a yes/no to the browser, never the cookie's value", async () => {
    jar.names.add("arkray_session");
    const props = providersProps(await RootLayout({ children: <p>page</p> }));
    expect(Object.keys(props).sort()).toEqual(["children", "sessionAtLoad"]);
  });
});
