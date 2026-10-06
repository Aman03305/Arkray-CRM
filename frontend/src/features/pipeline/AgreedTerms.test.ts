import { describe, expect, it } from "vitest";

import { parseAgreedTerms } from "./AgreedTerms";

describe("parseAgreedTerms (ADR-0029)", () => {
  it("needs both, and says what is missing at once", () => {
    expect(parseAgreedTerms("", "")).toEqual({
      ok: false,
      errors: { price: ["Enter the agreed price."], cpt: ["Enter the agreed CPT."] },
    });
    expect(parseAgreedTerms("11,00,000", "   ")).toEqual({ ok: false, errors: { cpt: ["Enter the agreed CPT."] } });
    expect(parseAgreedTerms("", "Rs 18")).toEqual({ ok: false, errors: { price: ["Enter the agreed price."] } });
  });

  it("sends the price as an exact decimal and the CPT as one line", () => {
    expect(parseAgreedTerms("₹ 10,50,000.50", "  Rs 18\n per   test ")).toEqual({
      ok: true,
      value: { price: "1050000.50", cpt: "Rs 18 per test" },
    });
  });

  it("explains a price that isn't plain rupees, and a CPT that is too long", () => {
    const parsed = parseAgreedTerms("1e6", "x".repeat(101));
    expect(parsed.ok).toBe(false);
    if (parsed.ok) return;
    expect(parsed.errors.price?.[0]).toMatch(/Enter an amount in rupees/);
    expect(parsed.errors.cpt).toEqual(["Use at most 100 characters."]);
    expect(parseAgreedTerms("1", "x".repeat(100)).ok).toBe(true);
  });
});
