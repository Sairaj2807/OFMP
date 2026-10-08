import { describe, expect, it } from "vitest";

import type { MarketProfile, ProfileHistory, ProfileRow } from "../types";
import {
  clampScroll, collectColumns, computeMultiLayout, type DayColumn, followScroll, mapper, priceGrid, rowAt, rowOf,
} from "./multiDay";

const ROW = 5;

/** A profile covering [low, high] in 5-point rows, high to low, letters by distance from the middle. */
function profile(date: string, low: number, high: number, close = high): MarketProfile {
  const rows: ProfileRow[] = [];
  for (let p = high; p >= low; p -= ROW) rows.push({ price: p, letters: "ABC", tpo: 3, volume: p, buy: p, sell: 0, delta: p });
  return { date, row_size: ROW, rows, stats: { open: low, high, low, close, tpo_total: rows.length * 3, volume: 1,
                                                delta: 0, trades: 1 }, periods: [] };
}

const history = (sessions: MarketProfile[], liveDate: string): ProfileHistory => ({
  symbol: "NIFTYOCT", row_size: ROW, days: 5, live_date: liveDate, excluded: [],
  sessions: sessions.map((p) => ({ date: p.date!, state: "historical", profile: p })),
});

// Day 1: 24500-24700, Day 2: 24600-24900, Day 3: 24800-25100 (the shared-axis fixture)
const d1 = profile("2026-10-06", 24500, 24700);
const d2 = profile("2026-10-07", 24600, 24900);
const d3 = profile("2026-10-08", 24800, 25100, 24950);
const columns = (): DayColumn[] => collectColumns(history([d2, d1], "2026-10-08"), d3);

describe("multi-day market profile coordinates", () => {
  it("orders sessions oldest first with today last, live, and never fabricates days", () => {
    const cols = columns();
    expect(cols.map((c) => [c.date, c.state])).toEqual([
      ["2026-10-06", "historical"], ["2026-10-07", "historical"], ["2026-10-08", "live"]]);
    // after a rollover, history at/after the live date is dropped; an empty today still has a column
    const rolled = collectColumns(history([d1, d2, d3], "2026-10-08"), { date: "2026-10-08", rows: [] });
    expect(rolled.map((c) => c.date)).toEqual(["2026-10-06", "2026-10-07", "2026-10-08"]);
    expect(rolled[2].profile).toBeNull();
  });

  it("builds one price grid from the union of all days, on the absolute row grid", () => {
    const grid = priceGrid(columns(), ROW)!;
    expect([grid.topRow, grid.bottomRow, grid.rows]).toEqual([rowOf(25100, ROW), rowOf(24500, ROW), 121]);
    expect(rowOf(24700, ROW)).toBe(4940);
    expect(rowOf(24704.9, ROW)).toBe(4940);
  });

  it("maps the same price to the same Y in every day, and days to different X", () => {
    const cols = columns();
    const grid = priceGrid(cols, ROW)!;
    const l = computeMultiLayout(cols, grid, 900, 600, "stacked");
    const m = mapper(l, grid, { x: 0, y: 0 }, cols.length);
    // 24700 exists in day 1 (its top), day 2 (inside) and ... day 3 does not reach it: same Y wherever drawn
    const ys = cols.map((c) => (rowAt(c, 24700, ROW) ? m.priceToY(24700) : null));
    expect(ys[0]).toBe(ys[1]);
    expect(ys[2]).toBeNull();
    expect(m.priceToY(24850)).toBe(m.priceToY(24854.9));                  // same row band
    expect(m.priceToY(24700) - m.priceToY(24705)).toBeCloseTo(l.rowPx, 9);         // one row apart
    expect([m.sessionToX(0), m.sessionToX(1), m.sessionToX(2)]).toEqual([0, l.colW, 2 * l.colW]);
    // each day's own rows land at the shared Y of their absolute price
    for (const c of cols) {
      for (const r of c.profile!.rows) expect(m.priceToY(r.price)).toBe(m.priceToY(r.price + 0.05));
    }
  });

  it("hit-tests session first, then price, then that session's own row", () => {
    const cols = columns();
    const grid = priceGrid(cols, ROW)!;
    const l = computeMultiLayout(cols, grid, 900, 600, "stacked");
    const m = mapper(l, grid, { x: 0, y: 0 }, cols.length);
    const y = m.priceToY(24650) + 1;
    expect(m.yToPrice(y)).toBe(24650);
    expect(m.xToSession(5)).toBe(0);
    expect(m.xToSession(l.colW + 5)).toBe(1);
    expect(m.xToSession(l.axisX + 1)).toBeNull();                      // the price axis is not a session
    expect(rowAt(cols[0], 24650, ROW)?.price).toBe(24650);
    expect(rowAt(cols[2], 24650, ROW)).toBeNull();                      // day 3 never traded 24650
    expect(m.yToPrice(l.plotTop - 1)).toBeNull();                       // header
  });

  it("horizontal scroll moves sessions without changing the price mapping; vertical moves price", () => {
    const cols = [...Array(12)].map((_, i) => ({ ...columns()[i % 3], date: `2026-09-${10 + i}` }));
    const grid = priceGrid(cols, ROW)!;
    const l = computeMultiLayout(cols, grid, 700, 300, "split");
    expect(l.maxScrollX).toBeGreaterThan(0);
    const a = mapper(l, grid, { x: 0, y: 3 }, cols.length);
    const b = mapper(l, grid, { x: l.colW * 4, y: 3 }, cols.length);
    expect(b.priceToY(24800)).toBe(a.priceToY(24800));
    expect(b.sessionToX(4)).toBe(0);
    expect(b.visibleSessions()[0]).toBe(4);
    const c = mapper(l, grid, { x: 0, y: 4 }, cols.length);
    expect(a.priceToY(24800) - c.priceToY(24800)).toBeCloseTo(l.rowPx, 9);
    expect(clampScroll({ x: -5, y: 1e9 }, l)).toEqual({ x: 0, y: l.maxScrollY });
  });

  it("follows today: rightmost column, last price centred, axis unchanged", () => {
    const cols = [...Array(10)].map((_, i) => ({ ...columns()[i % 3], date: `2026-09-${10 + i}` }));
    const grid = priceGrid(cols, ROW)!;
    const l = computeMultiLayout(cols, grid, 600, 200, "stacked");
    const s = followScroll(l, grid, 24950);
    expect(s.x).toBe(l.maxScrollX);
    const m = mapper(l, grid, s, cols.length);
    const mid = l.plotTop + l.plotH / 2;
    expect(Math.abs(m.priceToY(24950) - mid)).toBeLessThanOrEqual(l.rowPx);
  });

  it("split mode gives each day room for every period; stacked is narrower", () => {
    const withPeriods = columns().map((c) => ({ ...c, profile: { ...c.profile!, periods: [...Array(13)].map((_, i) =>
      ({ index: i, letter: "ABCDEFGHIJKLM"[i], start_ms: 0, high: 0, low: 0, open: 0, close: 0 })) } }));
    const grid = priceGrid(withPeriods, ROW)!;
    const split = computeMultiLayout(withPeriods, grid, 900, 600, "split");
    const stacked = computeMultiLayout(withPeriods, grid, 900, 600, "stacked");
    expect(split.tpoCols).toBe(13);
    expect(stacked.tpoCols).toBe(3);
    expect(split.colW).toBeGreaterThan(stacked.colW);
    const m = mapper(split, grid, { x: 0, y: 0 }, withPeriods.length);
    expect(m.tpoX(1, 0, "C") - m.sessionToX(1)).toBe(14 + 2 * split.cellW);   // C: its own column in day 2
  });
});
