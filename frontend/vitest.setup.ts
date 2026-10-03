import "@testing-library/jest-dom/vitest";
import { cleanup, configure } from "@testing-library/react";
import { afterEach } from "vitest";

// findBy*/waitFor give slow CI machines (and parallel workers) time to render large forms;
// assertions are unchanged, a genuinely missing element still fails.
configure({ asyncUtilTimeout: 4000 });

afterEach(() => {
  cleanup();
  // Tests must not leak cookies (e.g. the CSRF token) into each other.
  for (const cookie of document.cookie.split(";")) {
    const name = cookie.split("=")[0]?.trim();
    if (name) document.cookie = `${name}=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/`;
  }
});
