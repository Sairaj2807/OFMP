import { describe, expect, it } from "vitest";

import type { MarketProfile, ProfileHistory, ProfileRow } from "../types";
import {
  clampViewport, COL_SCALE, collectColumns, computeLayout, type DayColumn, fitZoom, followView, labelStep, mapper,
  panBy, priceGrid, ROW_PX, rowAt, rowOf, type Viewport, visibleSpan, zoomX, zoomY,
} from "./multiDay";

const ROW = 5;

function profile(date: string, low: number, high: number, close = high, letters = "ABC"): MarketProfile {
  const rows: ProfileRow[] = [];
  for (let p = high; p >= low; p -= ROW) rows.push({ price: p, letters, tpo: letters.length, volume: p, buy: p, sell: 0, delta: p });
  return { date, row_size: ROW, rows, periods: [],
           stats: { open: low, high, low, close, tpo_total: rows.length * 3, volume: 1, delta: 0, trades: 1 } };
}

const history = (sessions: MarketProfile[], liveDate: string): ProfileHistory => ({
  symbol: "NIFTYOCT", row_size: ROW, days: 5, live_date: liveDate, excluded: [],
  sessions: sessions.map((p) => ({ date: p.date!, state: "historical", profile: p })),
});

// Day 1: 24500-24700, Day 2: 24600-24900, Day 3 (live): 24800-25100 (the shared-axis fixture)
const d1 = profile("2026-10-06", 24500, 24700);
const d2 = profile("2026-10-07", 24600, 24900);
const d3 = profile("2026-10-08", 24800, 25100, 24950);
const columns = (): DayColumn[] => collectColumns(history([d2, d1], "2026-10-08"), d3);
const many = (n: number) => [...Array(n)].map((_, i) => ({ ...columns()[i % 3], date: `2026-09-${10 + i}` }));

function setup(cols: DayColumn[], w = 900, h = 600, mode: "stacked" | "split" = "stacked", vp?: Partial<Viewport>) {
  const grid = priceGrid(cols, ROW)!;
  const zoom = fitZoom(cols, grid, w, h, mode);
  let view: Viewport = { topPrice: grid.high, leftX: 0, follow: true, ...zoom, ...vp };
  let l = computeLayout(cols, w, h, mode, view);
  view = clampViewport(view, l, grid);
  l = computeLayout(cols, w, h, mode, view);
  return { grid, l, view, m: mapper(l, grid, view, cols.length) };
}

describe("columns and the shared price grid", () => {
  it("orders sessions oldest first with the live day last, and never fabricates days", () => {
    expect(columns().map((c) => [c.date, c.state])).toEqual([
      ["2026-10-06", "historical"], ["2026-10-07", "historical"], ["2026-10-08", "live"]]);
    const rolled = collectColumns(history([d1, d2, d3], "2026-10-08"), { date: "2026-10-08", rows: [] });
    expect(rolled.map((c) => c.date)).toEqual(["2026-10-06", "2026-10-07", "2026-10-08"]);
    expect(rolled[2].profile).toBeNull();
    const replay = collectColumns(history([d1], "2026-10-08"), d2, true);       // replay: that session only
    expect(replay.map((c) => [c.date, c.replay])).toEqual([["2026-10-07", true]]);
  });

  it("is the union of all days on the absolute row grid", () => {
    const grid = priceGrid(columns(), ROW)!;
    expect([grid.topRow, grid.bottomRow, grid.rows, grid.high, grid.low]).toEqual(
      [rowOf(25100, ROW), rowOf(24500, ROW), 121, 25105, 24500]);
  });
});

describe("coordinates", () => {
  it("maps the same price to the same Y in every day, and days to different X", () => {
    const cols = columns();
    const { l, m } = setup(cols);
    expect(rowAt(cols[0], 24700, ROW) && rowAt(cols[1], 24700, ROW)).toBeTruthy();
    expect(rowAt(cols[2], 24700, ROW)).toBeNull();                         // day 3 never traded 24700
    expect(m.priceToY(24850)).toBe(m.priceToY(24854.9));                  // same row band
    expect(m.priceToY(24700) - m.priceToY(24705)).toBeCloseTo(l.rowPx, 9);
    const xs = [0, 1, 2].map(m.sessionToX);
    expect(xs[1] - xs[0]).toBeCloseTo(l.colW, 9);
    expect(xs[2] - xs[1]).toBeCloseTo(l.colW, 9);
  });

  it("hit-tests session first, then price, then that session's own row", () => {
    const cols = columns();
    const { l, m } = setup(cols);
    expect(m.yToPrice(m.priceToY(24650) + 1)).toBe(24650);
    expect(m.xToSession(m.sessionToX(0) + 5)).toBe(0);
    expect(m.xToSession(m.sessionToX(1) + 5)).toBe(1);
    expect(m.xToSession(l.axisX + 1)).toBeNull();
    expect(rowAt(cols[2], 24650, ROW)).toBeNull();
    expect(m.yToPrice(l.plotTop - 1)).toBeNull();
  });
});

describe("viewport: zoom, pan, fit, follow", () => {
  it("zooming the price axis keeps the price under the cursor at the same pixel, and changes nothing else", () => {
    const cols = many(10);
    const { grid, l, view } = setup(cols, 700, 500, "stacked", { follow: false });
    const anchorY = 260;
    const before = mapper(l, grid, view, cols.length);
    const priceAt = view.topPrice - ((anchorY - l.plotTop) / l.rowPx) * ROW;
    for (const f of [2, 0.5, 1.7]) {
      const z = zoomY(view, f, anchorY, l, ROW);
      const lz = computeLayout(cols, 700, 500, "stacked", z);
      expect(z.topPrice - ((anchorY - lz.plotTop) / lz.rowPx) * ROW).toBeCloseTo(priceAt, 6);
      const mz = mapper(lz, grid, z, cols.length);
      expect(mz.sessionToX(3)).toBe(before.sessionToX(3));                // X untouched
      expect(z.follow).toBe(false);
    }
    expect(zoomY(view, 1000, anchorY, l, ROW).rowPx).toBe(ROW_PX.max);
    expect(zoomY(view, 0.0001, anchorY, l, ROW).rowPx).toBe(ROW_PX.min);
  });

  it("zooming the day axis keeps the content under the cursor in place; the price mapping is untouched", () => {
    const cols = many(12);
    const { grid, l, view } = setup(cols, 700, 500, "split", { follow: false, leftX: 300 });
    const anchorX = 250;
    const contentX = anchorX + view.leftX;
    const z = zoomX(view, 1.8, anchorX);
    const lz = computeLayout(cols, 700, 500, "split", z);
    expect(contentX * (lz.colW / l.colW) - z.leftX).toBeCloseTo(anchorX, 6);
    const a = mapper(l, grid, view, cols.length), b = mapper(lz, grid, z, cols.length);
    expect(b.priceToY(24800)).toBe(a.priceToY(24800));
    expect(lz.cellW).toBeCloseTo(l.cellW * 1.8, 6);
    expect(zoomX(view, 100, anchorX).colScale).toBe(COL_SCALE.max);
  });

  it("panning moves both axes without changing zoom; the same price stays aligned across days", () => {
    const cols = many(12);
    const { grid, l, view } = setup(cols, 700, 400, "stacked", { follow: false });
    const p = panBy(view, -120, 50, l, ROW);
    expect([p.rowPx, p.colScale]).toEqual([view.rowPx, view.colScale]);
    const a = mapper(l, grid, view, cols.length), b = mapper(l, grid, p, cols.length);
    expect(b.priceToY(24800) - a.priceToY(24800)).toBeCloseTo(50, 6);    // content follows the pointer
    expect(b.sessionToX(4) - a.sessionToX(4)).toBeCloseTo(-120, 6);
  });

  it("clamps to the data, centres short ranges and right-aligns narrow content", () => {
    const cols = many(12);
    const { grid, l, view } = setup(cols, 700, 400, "stacked", { follow: false });
    const far = clampViewport({ ...view, topPrice: 1e9, leftX: 1e9 }, l, grid);
    expect(far.topPrice).toBeCloseTo(grid.high + visibleSpan(l, ROW) / 2, 6);
    expect(far.leftX).toBeCloseTo(l.contentW - l.axisX / 2, 6);
    const one = [columns()[2]];                                             // single day, short range
    const s = setup(one, 900, 2000);
    expect(s.l.contentW).toBeLessThanOrEqual(s.l.axisX);
    expect(s.view.leftX).toBeCloseTo(s.l.contentW - s.l.axisX, 6);         // the day sits by the price axis
    const mid = (s.grid.high + s.grid.low) / 2;
    expect(s.view.topPrice).toBeCloseTo(mid + visibleSpan(s.l, ROW) / 2, 6);
  });

  it("fits a single day to the width and the range to the height; many days keep 1x and scroll", () => {
    const one = setup([columns()[2]], 900, 600);
    expect(one.view.colScale).toBeGreaterThan(1);
    const twenty = setup(many(20), 900, 600);
    expect(twenty.view.colScale).toBe(1);
    expect(twenty.l.contentW).toBeGreaterThan(twenty.l.axisX);
    expect(one.view.rowPx).toBeGreaterThanOrEqual(ROW_PX.fitMin);
  });

  it("follow puts today's column in view and its last price at the centre", () => {
    const cols = many(10);
    const { grid, l, view } = setup(cols, 600, 220);
    const f = followView(view, l, grid, 24950);
    const m = mapper(l, grid, f, cols.length);
    expect(f.follow).toBe(true);
    expect(f.leftX).toBeCloseTo(l.contentW - l.axisX, 6);
    expect(Math.abs(m.priceToY(24950) + l.rowPx / 2 - (l.plotTop + l.plotH / 2))).toBeLessThanOrEqual(l.rowPx);
  });

  it("level of detail follows the pixels: letters, blocks, thin bars; volume hides when too narrow", () => {
    const cols = columns();
    const grid = priceGrid(cols, ROW)!;
    const big = computeLayout(cols, 900, 600, "stacked", { rowPx: 20, colScale: 2 });
    const small = computeLayout(cols, 900, 600, "stacked", { rowPx: 5, colScale: 1 });
    const tiny = computeLayout(cols, 900, 600, "stacked", { rowPx: 2, colScale: 0.35 });
    expect([big.drawLetters, small.drawLetters, tiny.thinBars]).toEqual([true, false, true]);
    expect([big.showVolume, tiny.showVolume]).toEqual([true, false]);
    expect(grid.rows).toBe(121);
  });

  it("price labels use round steps that never crowd", () => {
    expect(labelStep(20, 5)).toBe(5);
    expect(labelStep(4, 5)).toBe(20);
    expect(labelStep(2, 1)).toBe(10);
    expect(labelStep(30, 10)).toBe(10);
  });
});
