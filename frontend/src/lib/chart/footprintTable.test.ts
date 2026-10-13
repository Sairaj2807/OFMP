import { describe, expect, it } from "vitest";

import type { EngineBar } from "../types";
import { cellColor, cellTexts, compact, FootprintTable, rowValue, TABLE_COLORS, TABLE_ROWS, tableHeight } from "./footprintTable";

const bar: EngineBar = { time: 1, delta: -310, cells: [], volume: 12_480, cot: -310, vpoc: 24015, vpoc_volume: 1_950,
                         oi: 18_420_150, doi: 6_500 };

describe("footprint table", () => {
  it("reads each row from the server's fields; unknown stays unknown", () => {
    expect(TABLE_ROWS.map((r) => rowValue(r, bar))).toEqual([12_480, 24015, 1_950, -310, 18_420_150, 6_500]);
    expect(rowValue("doi", { ...bar, doi: null })).toBeNull();
    expect(rowValue("oi", { time: 1, delta: 0, cells: [] })).toBeNull();          // a server without the table
  });

  it("formats full, then compact for narrow columns; quantities follow the lot divisor", () => {
    expect(cellTexts("volume", 12_480, 1)).toEqual(["12,480", "12.5K"]);
    expect(cellTexts("oi", 18_420_150, 1)).toEqual(["18,420,150", "18.4M"]);
    expect(cellTexts("oi", 18_420_155, 65)).toEqual(["283,387", "283.4K"]);       // lots (OI is whole lots)
    expect(cellTexts("cot", -310, 1)).toEqual(["−310", "−310"]);
    expect(cellTexts("doi", 6_500, 65)).toEqual(["+100", "+100"]);
    expect(cellTexts("vpoc", 24015, 65)).toEqual(["24015"]);                    // a price: never divided
    expect(cellTexts("doi", null, 1)).toEqual(["—"]);
    expect([999, 1_234, 56_789, 1_234_567].map(compact)).toEqual(["999", "1.23K", "56.8K", "1.23M"]);
  });

  it("colours COT and dOI by sign and VPOC pink", () => {
    expect(cellColor("cot", 5)).toBe(TABLE_COLORS.buy);
    expect(cellColor("doi", -1)).toBe(TABLE_COLORS.sell);
    expect(cellColor("doi", 0)).toBe(TABLE_COLORS.text);
    expect(cellColor("vpoc", 24015)).toBe(TABLE_COLORS.vpoc);
    expect(cellColor("oi", null)).toBe(TABLE_COLORS.label);
  });

  it("takes no space when hidden or without rows, and keeps its fixed row order", () => {
    const t = new FootprintTable();
    expect(t.height()).toBe(tableHeight(TABLE_ROWS));
    t.setOptions({ rows: ["doi", "volume"] });
    expect(t.height()).toBe(tableHeight(["volume", "doi"]));
    t.setOptions({ visible: false });
    expect(t.height()).toBe(0);
    t.setOptions({ visible: true, rows: [] });
    expect(t.height()).toBe(0);
  });
});
