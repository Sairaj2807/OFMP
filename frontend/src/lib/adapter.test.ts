import { describe, expect, it } from "vitest";

import { bandRange, cvdAfter, hasOhlc, toCandle, toFootprintBar } from "./adapter";
import type { EngineBar } from "./types";

const bar: EngineBar = {
  time: 60, open: 100, high: 102, low: 99, close: 101, delta: -5, min_delta: -8, max_delta: 3, trades: 7,
  cells: [{ price: 101, buy: 10, sell: 4 }, { price: 100, buy: 1, sell: 12 }],
};

describe("adapter (engine -> chart library)", () => {
  it("maps BUY to askVol and SELL to bidVol, centred in the price band", () => {
    const out = toFootprintBar(bar, 1);
    expect(out.cells[0]).toEqual({ price: 101.5, bidVol: 4, askVol: 10 });
    expect(out.delta).toBe(-5);
    expect([out.tradeCount, out.minDelta, out.maxDelta]).toEqual([7, -8, 3]);
  });

  it("never turns missing metadata into zeros", () => {
    const out = toFootprintBar({ time: 1, delta: 0, cells: [], open: null, trades: null, min_delta: null }, 1);
    for (const k of ["open", "high", "tradeCount", "minDelta", "maxDelta"]) expect(k in out).toBe(false);
    expect(hasOhlc({ time: 1, delta: 0, cells: [], open: 1, high: 1, low: 1, close: null })).toBe(false);
  });

  it("builds candles with total volume and computes CVD", () => {
    expect(hasOhlc(bar) && toCandle(bar).volume).toBe(27);
    expect(cvdAfter(100, [bar, { ...bar, delta: 20 }])).toBe(115);
  });

  it("band range covers the traded prices of a cell", () => {
    expect(bandRange(101.5, 1, 0.1)).toEqual([101, 101.9]);
    expect(bandRange(100.05, 0.1, 0.1)).toEqual([100, 100]);
  });
});
