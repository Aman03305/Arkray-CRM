import { describe, expect, it, vi } from "vitest";

import {
  amountInputValue,
  formatInr,
  formatInrCompact,
  formatPercent,
  groupIndian,
  parseAmountInput,
  parsePercentInput,
  sameDecimal,
} from "./money";

describe("formatting amounts exactly", () => {
  it.each([
    ["0.00", "₹0"],
    ["1.00", "₹1"],
    ["999.00", "₹999"],
    ["1000.00", "₹1,000"],
    ["50000.00", "₹50,000"],
    ["120000.00", "₹1,20,000"],
    ["1250000.00", "₹12,50,000"],
    ["1250000.50", "₹12,50,000.50"],
    ["1250000.05", "₹12,50,000.05"],
    ["10000000.00", "₹1,00,00,000"],
    ["999999999999.99", "₹9,99,99,99,99,999.99"],
    // Beyond 2^53 a Number would lose the paise; the string never does.
    ["24999999999999.75", "₹2,49,99,99,99,99,999.75"],
    ["9007199254740993.01", "₹9,00,71,99,25,47,40,993.01"],
  ])("%s -> %s", (value, shown) => {
    expect(formatInr(value)).toBe(shown);
  });

  it("can always show paise", () => {
    expect(formatInr("1250000.00", { paise: "always" })).toBe("₹12,50,000.00");
  });

  it("never guesses at something that isn't a decimal string", () => {
    for (const bad of ["", "abc", "1e6", "NaN", "Infinity", "1,000", " "]) expect(formatInr(bad)).toBe("—");
    expect(formatInr(null)).toBe("—");
    expect(formatInr(undefined)).toBe("—");
  });

  it("never turns an amount into a JavaScript number", () => {
    const toNumber = vi.spyOn(globalThis, "Number");
    const parse = vi.spyOn(globalThis, "parseFloat");
    formatInr("9007199254740993.01");
    formatInrCompact("9007199254740993.01");
    parseAmountInput("90,07,19,92,54,74");
    expect(toNumber).not.toHaveBeenCalled();
    expect(parse).not.toHaveBeenCalled();
    toNumber.mockRestore();
    parse.mockRestore();
  });

  it("groups digits the Indian way", () => {
    expect(groupIndian("1")).toBe("1");
    expect(groupIndian("12345")).toBe("12,345");
    expect(groupIndian("123456")).toBe("1,23,456");
    expect(groupIndian("0001234")).toBe("1,234");
  });

  it.each([
    ["75000.00", "₹75,000"],
    ["125000.00", "₹1.2 L"],
    ["1250000.00", "₹12.5 L"],
    ["10000000.00", "₹1 Cr"],
    ["32999999.99", "₹3.2 Cr"], // truncated, never rounded up to 3.3
    ["1234567890000.00", "₹1,23,456.7 Cr"],
  ])("compact %s -> %s", (value, shown) => {
    expect(formatInrCompact(value)).toBe(shown);
  });
});

describe("percentages", () => {
  it.each([
    ["75.00", "75%"],
    ["62.50", "62.5%"],
    ["33.33", "33.33%"],
    ["0.00", "0%"],
    ["100.00", "100%"],
  ])("%s -> %s", (value, shown) => {
    expect(formatPercent(value)).toBe(shown);
  });
});

describe("parsing what people type", () => {
  it.each([
    ["1250000", "1250000"],
    ["12,50,000", "1250000"],
    ["1,250,000.50", "1250000.50"],
    ["₹ 12,50,000", "1250000"],
    ["Rs. 500", "500"],
    ["0", "0"],
    ["007", "7"],
    ["1250000.5", "1250000.5"],
    ["  999999999999.99 ", "999999999999.99"],
  ])("%s -> %s", (typed, sent) => {
    expect(parseAmountInput(typed)).toEqual({ ok: true, value: sent });
  });

  it.each(["", "abc", "-5", "+5", "1e6", "1.2E+6", "1.001", "0x10", "١٢٣", "１２３", "1000000000000", "12.5.5", "NaN", "Infinity"])(
    "refuses %j",
    (typed) => {
      expect(parseAmountInput(typed).ok).toBe(false);
    },
  );

  it.each([
    ["50", "50"],
    ["62.5", "62.5"],
    ["62.50%", "62.50"],
    ["100", "100"],
    ["100.00", "100.00"],
    ["0", "0"],
  ])("percentage %s -> %s", (typed, sent) => {
    expect(parsePercentInput(typed)).toEqual({ ok: true, value: sent });
  });

  it.each(["100.01", "101", "-1", "1e1", "50.555", "", "abc"])("refuses the percentage %j", (typed) => {
    expect(parsePercentInput(typed).ok).toBe(false);
  });

  it("prefills an amount field without trailing zeros", () => {
    expect(amountInputValue("1250000.00")).toBe("1250000");
    expect(amountInputValue("1250000.50")).toBe("1250000.5");
    expect(amountInputValue(null)).toBe("");
  });

  it("compares decimals as text", () => {
    expect(sameDecimal("50", "50.00")).toBe(true);
    expect(sameDecimal("050.0", "50")).toBe(true);
    expect(sameDecimal("50.01", "50")).toBe(false);
    expect(sameDecimal("", "")).toBe(false);
  });
});
