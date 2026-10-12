import { describe, expect, it } from "vitest";

import { letterIndex, periodColor } from "./profileLayout";

describe("period colours", () => {
  it("indexes letters A-Z then a-z and gives each period its own colour", () => {
    expect([letterIndex("A"), letterIndex("M"), letterIndex("a"), letterIndex("?")]).toEqual([0, 12, 26, 0]);
    expect(periodColor(0)).not.toBe(periodColor(1));
    expect(periodColor(0)).toBe(periodColor(0));
  });
});
