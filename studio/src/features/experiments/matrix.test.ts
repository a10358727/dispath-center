import { describe, expect, it } from "vitest";
import { expandCombinations, expandedRunCount, parseAxisValues } from "./matrix";

describe("matrix expansion (mirrors app/experiment_v2.expand_matrix)", () => {
  it("orders axes by name and preserves declared value order", () => {
    const combos = expandCombinations([
      { name: "lr", values: ["0.1", "0.01"] },
      { name: "batch", values: [16, 32] },
    ]);
    expect(combos).toEqual([
      { batch: 16, lr: "0.1" },
      { batch: 16, lr: "0.01" },
      { batch: 32, lr: "0.1" },
      { batch: 32, lr: "0.01" },
    ]);
    expect(expandedRunCount([{ name: "a", values: [1, 2, 3] }, { name: "b", values: [true, false] }])).toBe(6);
    expect(expandedRunCount([])).toBe(0);
  });

  it("parses values per parameter type and keeps numbers as decimal strings", () => {
    expect(parseAxisValues("16, 32,16", "integer").values).toEqual([16, 32]);
    expect(parseAxisValues("0.1\n0.01", "number").values).toEqual(["0.1", "0.01"]);
    expect(parseAxisValues("true,false", "boolean").values).toEqual([true, false]);
    expect(parseAxisValues("adam, sgd", "enum").values).toEqual(["adam", "sgd"]);
    expect(parseAxisValues("x1", "integer").error).toContain("整數");
    expect(parseAxisValues("  ", "string").error).toBeTruthy();
  });
});
