// Engine payload -> chart-library shapes. Port of static/orderflow_adapter.js;
// the ONE place the engine's vocabulary meets the library's.
//
// - The engine says BUY / SELL in the project's Vtrender-polarity convention
//   (a print at the bid is BUY). BUY maps to `askVol` (drawn right) and SELL
//   to `bidVol` (drawn left), which keeps the library's delta (ask - bid)
//   equal to the engine's (buy - sell). It does NOT mean "aggressor at the
//   ask", so the UI never shows the library's own "bid"/"ask" labels.
// - Engine rows are labelled by their price-band FLOOR; the library centres a
//   cell on its price, so the band centre is sent.
import type { CandleData } from "@/vendor/openalgo-charts/openalgo-charts.mjs";
import type { FootprintBar } from "@/vendor/openalgo-charts/openalgo-charts.profile.mjs";

import type { EngineBar } from "./types";

const round8 = (v: number) => Math.round(v * 1e8) / 1e8;

export function hasOhlc(bar: EngineBar): bar is EngineBar & { open: number; high: number; low: number; close: number } {
  return bar.open != null && bar.high != null && bar.low != null && bar.close != null;
}

export function toFootprintBar(bar: EngineBar, rowSize: number): FootprintBar {
  const out: FootprintBar = {
    time: bar.time,
    cells: bar.cells.map((c) => ({ price: round8(c.price + rowSize / 2), bidVol: c.sell, askVol: c.buy })),
    delta: bar.delta,
    rowSize,
  };
  // Optional metadata: absent means "unknown", never a fabricated zero.
  if (hasOhlc(bar)) {
    out.open = bar.open;
    out.high = bar.high;
    out.low = bar.low;
    out.close = bar.close;
  }
  if (bar.trades != null) out.tradeCount = bar.trades;
  if (bar.min_delta != null) out.minDelta = bar.min_delta;
  if (bar.max_delta != null) out.maxDelta = bar.max_delta;
  return out;
}

/** The transparent price series giving the chart its time axis and autoscale. */
export function toCandle(bar: EngineBar & { open: number; high: number; low: number; close: number }): CandleData {
  let volume = 0;
  for (const c of bar.cells) volume += c.buy + c.sell;
  return { time: bar.time, open: bar.open, high: bar.high, low: bar.low, close: bar.close, volume };
}

/** [lowest, highest] traded price a band-centred cell stands for. */
export function bandRange(centerPrice: number, rowSize: number, tickSize: number): [number, number] {
  const lo = round8(centerPrice - rowSize / 2);
  return [lo, round8(lo + rowSize - tickSize)];
}

/** Session CVD after the newest bar: the server's offset plus every bar's delta. */
export function cvdAfter(cvdOffset: number, bars: EngineBar[]): number {
  return bars.reduce((sum, b) => sum + b.delta, cvdOffset);
}
