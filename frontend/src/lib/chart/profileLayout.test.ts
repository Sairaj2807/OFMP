import { describe, expect, it } from "vitest";

import type { MarketProfile } from "../types";
import { centreOn, computeLayout, labelEvery, letterIndex, MIN_ROW_PX, periodColor, rowIndexOf } from "./profileLayout";

const row = (price: number, letters: string, volume = 0) =>
  ({ price, letters, tpo: letters.length, volume, buy: volume, sell: 0, delta: volume });

const profile = (n: number): MarketProfile => ({
  date: "2026-09-29", row_size: 5,
  rows: Array.from({ length: n }, (_, i) => row(24600 - i * 5, "ABC".slice(0, 1 + (i % 3)), i * 10)),
});

describe("market profile layout", () => {
  it("fits every row when they fit and scrolls when they do not", () => {
    const small = computeLayout(profile(20), 600, 400);
    expect(small.rowPx).toBe(20);
    expect(small.maxScroll).toBe(0);
    expect(small.top).toBe(0);
    expect(computeLayout(profile(4), 600, 400).top).toBe(Math.floor((400 - 4 * 22) / 2));   // short: centred
    const big = computeLayout(profile(500), 600, 400);
    expect(big.rowPx).toBe(MIN_ROW_PX);
    expect(big.maxScroll).toBe(500 - big.visibleRows);
    expect(big.drawLetters).toBe(false);              // too small for text: TPO blocks
    expect(small.drawLetters).toBe(true);
    expect(big.maxVolume).toBe(4990);
    expect(small.volumeX).toBeGreaterThan(small.letterX + small.letterW);
    expect(small.axisX).toBe(600 - 64);
  });

  it("finds the row of a price by its band (rows labelled by their floor)", () => {
    const rows = profile(10).rows;                    // 24600 (top) .. 24555
    expect(rowIndexOf(rows, 24600, 5)).toBe(0);
    expect(rowIndexOf(rows, 24604.9, 5)).toBe(0);
    expect(rowIndexOf(rows, 24599.95, 5)).toBe(1);
    expect(rowIndexOf(rows, 24555, 5)).toBe(9);
    expect(rowIndexOf(rows, 24554.9, 5)).toBe(-1);
    expect(rowIndexOf([], 1, 5)).toBe(-1);
  });

  it("centres a row within the scroll range, labels without overlap, colours per period", () => {
    const l = computeLayout(profile(500), 600, 400);
    expect(centreOn(0, l)).toBe(0);
    expect(centreOn(499, l)).toBe(l.maxScroll);
    expect(centreOn(250, l)).toBe(250 - Math.round(l.visibleRows / 2));
    expect(labelEvery(4)).toBe(4);
    expect(labelEvery(20)).toBe(1);
    expect(letterIndex("A")).toBe(0);
    expect(letterIndex("M")).toBe(12);
    expect(letterIndex("a")).toBe(26);
    expect(periodColor(0)).not.toBe(periodColor(1));
  });
});
