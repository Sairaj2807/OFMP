import { describe, expect, it } from "vitest";

import type { MarketProfile } from "../types";
import { shortDate } from "./multiDay";
import {
  LEVEL_COLORS, levelsOnRow, nakedLabel, nakedToday, PROFILE_LEVELS, profileDrawing, structureBadges,
} from "./profileLevels";

const p: MarketProfile = {
  date: "2026-10-21", rows: [], poc_volume: 24510, poc_tpo: 24505, vwap: 24512.37, twap: 24505,
  value_area_volume: { low: 24500, high: 24520 }, value_area_tpo: { low: 24495, high: 24515 },
  hvn_bands: [{ low: 24530, high: 24535, peak: 24535 }],
  ib_extensions: { up: [{ multiple: 1.5, price: 24540 }, { multiple: 2, price: 24550 }, { multiple: 3, price: 24570 }],
                   down: [{ multiple: 1.5, price: 24470 }, { multiple: 2, price: 24460 }, { multiple: 3, price: 24440 }],
                   reached_up: 1.5, reached_down: null },
  dpoc: { price: 24510, trail: [{ period: 0, letter: "A", price: 24500 }, { period: 1, letter: "B", price: 24510 }] },
  weak_high: true, weak_low: false,
  previous: { date: "2026-10-20", poc_tpo: 24400, poc_volume: 24405, vah: 24420, val: 24390, high: 24450, low: 24380 },
};
const all = { enabled: PROFILE_LEVELS, showPoc: true, showValueArea: true, previous: true };

describe("profile levels", () => {
  it("draws V-VA and T-VA with pink POC bands and green VWAP / TWAP", () => {
    const d = profileDrawing(p, all);
    const byLabel = Object.fromEntries(d.lines.map((l) => [l.label, l]));
    expect(byLabel.VPOC).toMatchObject({ price: 24510, style: "band", color: LEVEL_COLORS.poc });
    expect(byLabel.TPOC).toMatchObject({ price: 24505, style: "band", color: LEVEL_COLORS.poc });
    expect(byLabel.VWAP).toMatchObject({ price: 24512.37, style: "thick", color: LEVEL_COLORS.avg, exact: true });
    expect(byLabel.TWAP).toMatchObject({ price: 24505, style: "thick", color: LEVEL_COLORS.avg, exact: false });
    expect([byLabel.VAH.price, byLabel.VAL.price, byLabel["TPO-VAH"].style]).toEqual([24520, 24500, "dashed"]);
    expect(d.shading.map((s) => s.label ?? s.fill)).toEqual([LEVEL_COLORS.vaV, LEVEL_COLORS.vaT, "HVN"]);
    expect(d.lines.filter((l) => l.label.endsWith("× IB")).map((l) => l.price)).toEqual(
      [24540, 24550, 24570, 24470, 24460, 24440]);
    expect(d.trail.map((t) => t.letter)).toEqual(["A", "B"]);
    expect(d.weak).toEqual(["high"]);
    expect(d.lines.filter((l) => l.color === LEVEL_COLORS.prev).map((l) => l.label)).toEqual(
      ["pPOC", "pVAH", "pVAL", "PDH", "PDL"]);
  });

  it("honours the Levels menu and the POC / VA buttons", () => {
    const vOnly = profileDrawing(p, { ...all, enabled: ["vva"], showPoc: false, showValueArea: false });
    expect(vOnly.lines.map((l) => l.label)).toEqual(["VAH", "VAL", "VWAP"]);       // no POC band, no shading
    expect(vOnly.shading).toEqual([]);
    expect(profileDrawing(p, { ...all, previous: false }).lines.some((l) => l.label === "PDH")).toBe(false);
    expect(profileDrawing({ date: null, rows: [] }, all)).toEqual({ shading: [], lines: [], trail: [], weak: [], boxes: [] });
  });

  it("names the levels on a hovered row; an exact VWAP counts for the row it falls in", () => {
    const d = profileDrawing(p, all);
    expect(levelsOnRow(d, 24510, 5)).toEqual(["VPOC", "VWAP"]);
    expect(levelsOnRow(d, 24505, 5)).toEqual(["TPOC", "TWAP"]);
    expect(levelsOnRow(d, 24530, 5)).toEqual(["HVN"]);
    expect(levelsOnRow(d, 24600, 5)).toEqual([]);
  });
});

describe("naked POCs today", () => {
  const npocs = [{ date: "2026-10-13", price: 22790, sessions_ago: 3 }, { date: "2026-10-16", price: 23120, sessions_ago: 2 }];
  const today = (low: number, high: number): MarketProfile =>
    ({ date: "2026-10-22", rows: [], stats: { open: low, high, low, close: high, tpo_total: 1, volume: 1, delta: 0, trades: 1 } });

  it("hides the ones today's range has reached (any part of their row)", () => {
    expect(nakedToday(npocs, today(22900, 23000), 5)).toEqual(npocs);
    expect(nakedToday(npocs, today(22794, 23000), 5).map((n) => n.price)).toEqual([23120]);   // inside the 22790 row
    expect(nakedToday(npocs, today(22700, 23120), 5)).toEqual([]);
    expect(nakedToday(npocs, null, 5)).toEqual(npocs);                                       // no trades yet today
  });

  it("labels each with its date and age in sessions", () => {
    expect(nakedLabel(npocs[0])).toBe(`nPOC ${shortDate("2026-10-13")} · 3d`);
  });
});

describe("day structure", () => {
  const st: MarketProfile = {
    date: "2026-10-21", rows: [],
    structure: {
      shape: "b", open_drive: "up",
      double_distribution: { lower: { low: 100, high: 110 }, upper: { low: 130, high: 140 }, thin: { low: 115, high: 125 } },
      value_area_rule: { opened: "above", active: true, target: 100, met: false, activated: 1, vah: 120, val: 100 },
      clusters: [{ low: 105, high: 110, volume: 500, delta: 20, periods: 4, absorption: true },
                 { low: 135, high: 140, volume: 400, delta: 200, periods: 3, absorption: false }],
    },
  };

  it("labels the day for the figures bar, following the Levels menu", () => {
    expect(structureBadges(st, PROFILE_LEVELS)).toEqual(
      ["b-shape", "Double distribution", "Open drive ↑", "80% rule active → 100", "2 clusters (1 absorption)"]);
    expect(structureBadges(st, ["varule"])).toEqual(["80% rule active → 100"]);
    const opened = { ...st, structure: { ...st.structure!, value_area_rule: { ...st.structure!.value_area_rule!, active: false } } };
    expect(structureBadges(opened, ["varule"])).toEqual(["Opened above prior VA"]);
    expect(structureBadges({ date: null, rows: [] }, PROFILE_LEVELS)).toEqual([]);
  });

  it("draws clusters, the two distributions with their thin zone, and the 80% target", () => {
    const d = profileDrawing(st, all);
    expect(d.boxes.map((b) => [b.label, b.low, b.dashed])).toEqual([
      ["absorption", 105, false], ["cluster", 135, true], ["upper distribution", 130, true], ["lower distribution", 100, true]]);
    expect(d.shading.find((x) => x.label === "thin zone")).toMatchObject({ low: 115, high: 125 });
    expect(d.lines.find((l) => l.label.startsWith("80%"))).toMatchObject({ label: "80% → VAL", price: 100 });
    expect(levelsOnRow(d, 105, 5)).toEqual(expect.arrayContaining(["absorption", "lower distribution"]));
  });
});
