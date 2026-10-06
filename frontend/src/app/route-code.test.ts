// @vitest-environment node
/**
 * Each page loads only its own module's code (2026-10-06). One shared views file made every
 * module page load every module: 764 KB of JavaScript (224 KB gzipped) on Dashboard, Pipeline
 * and Activities alike, where Settings needs 323 KB.
 */
import { readdirSync, readFileSync } from "node:fs";
import path from "node:path";

import { describe, expect, it } from "vitest";

const APP = path.join(process.cwd(), "src", "app");

function pages(): string[] {
  return readdirSync(APP, { recursive: true, encoding: "utf8" })
    .filter((file) => path.basename(file) === "page.tsx")
    .map((file) => path.join(APP, file));
}

describe("route code", () => {
  it("finds the pages", () => {
    expect(pages().length).toBeGreaterThan(20);
  });

  it("pages import their module's view file, never the index of every module", () => {
    const offenders = pages().filter((file) => /from "@\/features\/workspace\/views"/.test(readFileSync(file, "utf8")));
    expect(offenders.map((file) => path.relative(APP, file))).toEqual([]);
  });
});
