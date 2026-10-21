import { describe, expect, it } from "vitest";

import type { MarketProfile } from "../types";
import { LEVEL_COLORS, levelsOnRow, PROFILE_LEVELS, profileDrawing } from "./profileLevels";

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
    expect(profileDrawing({ date: null, rows: [] }, all)).toEqual({ shading: [], lines: [], trail: [], weak: [] });
  });

  it("names the levels on a hovered row; an exact VWAP counts for the row it falls in", () => {
    const d = profileDrawing(p, all);
    expect(levelsOnRow(d, 24510, 5)).toEqual(["VPOC", "VWAP"]);
    expect(levelsOnRow(d, 24505, 5)).toEqual(["TPOC", "TWAP"]);
    expect(levelsOnRow(d, 24530, 5)).toEqual(["HVN"]);
    expect(levelsOnRow(d, 24600, 5)).toEqual([]);
  });
});
