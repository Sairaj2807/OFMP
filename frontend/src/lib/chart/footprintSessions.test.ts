import { describe, expect, it, vi } from "vitest";

import type { EngineBar, FootprintDay, FootprintSessions } from "../types";
import {
  buildSessions, dayCap, effectiveDays, FootprintHistoryLoader, istDate, mergeBars, sessionStarts,
} from "./footprintSessions";
import { dayLabel } from "./sessionMarks";

// 2026-10-07 09:15 IST = 03:45 UTC
const T7 = Date.UTC(2026, 9, 7, 3, 45) / 1000, T8 = Date.UTC(2026, 9, 8, 3, 45) / 1000;
const bar = (time: number, delta = 1): EngineBar => ({ time, delta, cells: [], open: 1, high: 1, low: 1, close: 1 });
const day = (date: string, t0: number, n: number): FootprintDay =>
  ({ date, state: "historical", interval_sec: 60, row_size: 1, cvd_start: 0,
     bars: [...Array(n)].map((_, i) => bar(t0 + i * 60)) });

describe("footprint sessions", () => {
  it("caps days by interval: 1m 5, 3m 10, 5m and above 20", () => {
    expect([60, 180, 300, 900, 1800].map(dayCap)).toEqual([5, 10, 20, 20, 20]);
    expect(effectiveDays(20, 60)).toBe(5);
    expect(effectiveDays(10, 300)).toBe(10);
    expect(effectiveDays(0, 60)).toBe(0);
  });

  it("dates bars in IST and merges today: the live window wins, only that day kept", () => {
    expect(istDate(T7)).toBe("2026-10-07");
    expect(istDate(Date.UTC(2026, 9, 7, 18, 31) / 1000)).toBe("2026-10-08");   // 00:01 IST the next day
    const today = new Map<number, EngineBar>();
    mergeBars(today, [bar(T8, 1), bar(T8 + 60, 1)], "2026-10-08");
    mergeBars(today, [bar(T8 + 60, 5), bar(T8 + 120, 2), bar(T7, 9)], "2026-10-08");
    expect([...today.values()].map((b) => [b.time - T8, b.delta])).toEqual([[0, 1], [60, 5], [120, 2]]);
  });

  it("builds sessions oldest first with today last, each with its own bars and a CVD start of 0", () => {
    const days = new Map([["2026-10-07", day("2026-10-07", T7, 3)], ["2026-10-08", day("2026-10-08", T8, 2)]]);
    const today = new Map([[T8 + 600, bar(T8 + 600)], [T8, bar(T8)]]);
    const sessions = buildSessions(days, { date: "2026-10-08", bars: today });
    expect(sessions.map((s) => [s.date, s.live, s.bars.length, s.cvdStart])).toEqual([
      ["2026-10-07", false, 3, 0], ["2026-10-08", true, 2, 0]]);           // a history copy of today is ignored
    expect(sessions[1].bars.map((b) => b.time)).toEqual([T8, T8 + 600]);
    expect(sessionStarts(sessions)).toEqual([{ date: "2026-10-07", live: false, time: T7 },
                                             { date: "2026-10-08", live: true, time: T8 }]);
  });

  it("loads today and the newest day first, then older days one at a time up to the cap", async () => {
    const list: FootprintSessions = { symbol: "X", live_date: "2026-10-08", interval_sec: 60, max_days: 5,
      sessions: ["2026-09-30", "2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07"], excluded: [] };
    const dayFn = vi.fn(async (d: string) => day(d, T7, 1));
    const loader = new FootprintHistoryLoader({ sessions: async () => list, day: dayFn }, 60, 1, 20);
    await loader.start();
    expect(loader.cap).toBe(5);                                             // 1m: at most 5 days
    expect(loader.available).toEqual(list.sessions.slice(-5));
    expect(dayFn.mock.calls.map((c) => c[0])).toEqual(["2026-10-08", "2026-10-07"]);   // today, then the newest
    while (await loader.loadOlder()) { /* scrolling back */ }
    expect([...loader.days.keys()]).toEqual(["2026-10-07", "2026-10-06", "2026-10-05", "2026-10-02", "2026-10-01"]);
    expect(loader.exhausted).toBe(true);
    expect(await loader.loadOlder()).toBe(false);                           // never past the cap
    loader.dispose();
    expect(loader.stale()).toBe(true);
  });

  it("labels a session by its weekday and date", () => {
    expect(dayLabel("2026-10-08")).toBe("Thu 08 Oct");
    expect(dayLabel("not a date")).toBe("not a date");
  });
});
