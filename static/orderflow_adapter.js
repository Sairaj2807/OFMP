/* Engine payload -> chart-library shapes. Pure functions, no DOM, so they can be
   tested in Node against the real built library (tests/js/adapter.test.mjs).

   This file is the ONE place where the engine's vocabulary meets the library's:

   - The engine says BUY / SELL, in the project's Vtrender-polarity convention
     (a print at the bid is BUY). The library says ask / bid. BUY is mapped to
     `askVol` (drawn right, green) and SELL to `bidVol` (drawn left, red). That
     keeps the library's own delta (askVol - bidVol) equal to the engine's
     (buy - sell), and matches the existing footprint table's sell-left /
     buy-right layout. It does NOT mean "aggressor at the ask": the labels
     "bid" / "ask" the library prints in its own fixed strings are therefore
     avoided in the UI (see the tooltip and the table-row choices).

   - The engine's rows are labelled by their price-band FLOOR (24385 covers
     24385.0..24385.9 at 1 price/row). The library draws a cell centred on its
     price, so the band centre is sent. */

const round8 = (v) => Math.round(v * 1e8) / 1e8;

export function toFootprintBar(bar, rowSize) {
  const out = {
    time: bar.time,
    cells: bar.cells.map((c) => ({ price: round8(c.price + rowSize / 2), bidVol: c.sell, askVol: c.buy })),
    delta: bar.delta,
    rowSize,
  };
  // Optional metadata: absent means "unknown", never a fabricated zero.
  if (hasOhlc(bar)) {
    out.open = bar.open; out.high = bar.high; out.low = bar.low; out.close = bar.close;
  }
  if (bar.trades != null) out.tradeCount = bar.trades;
  if (bar.min_delta != null) out.minDelta = bar.min_delta;
  if (bar.max_delta != null) out.maxDelta = bar.max_delta;
  return out;
}

export function hasOhlc(bar) {
  return bar.open != null && bar.high != null && bar.low != null && bar.close != null;
}

/** The transparent price series that gives the chart its time axis and
    autoscale; the footprint draws its own candle beside the ladder. */
export function toCandle(bar) {
  let volume = 0;
  for (const c of bar.cells) volume += c.buy + c.sell;
  return { time: bar.time, open: bar.open, high: bar.high, low: bar.low, close: bar.close, volume };
}

/** [lowest, highest] traded price a band-centred cell stands for. */
export function bandRange(centerPrice, rowSize, tickSize) {
  const lo = round8(centerPrice - rowSize / 2);
  return [lo, round8(lo + rowSize - tickSize)];
}
