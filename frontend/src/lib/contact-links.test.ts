import { describe, expect, it } from "vitest";

import { mailtoHref } from "./contact-links";

describe("mailtoHref (Phase 9 review: mailto header injection)", () => {
  it("links an ordinary address as is", () => {
    expect(mailtoHref("rahul.sharma+crm@client.example")).toBe("mailto:rahul.sharma+crm@client.example");
  });

  it("encodes every character that would start or separate mailto headers", () => {
    const href = mailtoHref("x?to=sales%40client.com&bcc=spy%40evil.example&body=P9XSS&x=@client.com");
    expect(href.startsWith("mailto:")).toBe(true);
    const target = href.slice("mailto:".length);
    for (const delimiter of ["?", "&", "=", "#", ","]) expect(target).not.toContain(delimiter);
    // A mail client reads one recipient, with no Bcc and no body.
    const parsed = new URL(href);
    expect(parsed.search).toBe("");
    expect(decodeURIComponent(parsed.pathname)).toBe(
      "x?to=sales%40client.com&bcc=spy%40evil.example&body=P9XSS&x=@client.com",
    );
  });

  it("encodes spaces, quotes and angle brackets", () => {
    expect(mailtoHref('a b"<c>@d.example')).toBe("mailto:a%20b%22%3Cc%3E@d.example");
  });
});
