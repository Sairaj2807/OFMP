// Market profile levels (owner decisions 2026-10-21) as plain drawing data, so
// the canvas code stays a renderer and the choices are unit tested.
//
//   V-VA  VAH / VAL (volume), VPOC (thick pink band), VWAP (thick green band)
//   T-VA  TPO-VAH / TPO-VAL, TPOC (pink band), TWAP (green band)
//   HVN   thick brown bands: rows near the VPOC's volume, away from it
//   IB extensions 1.5x / 2x / 3x, the developing POC trail, weak highs / lows,
//   and the previous session's POC / VA / high / low.
// The server computes every value (backend/app/domain/profile/analytics.py).
import type { MarketProfile } from "../types";

export const PROFILE_LEVELS = ["vva", "tva", "hvn", "ibext", "dpoc", "weak", "prev"] as const;
export type ProfileLevel = (typeof PROFILE_LEVELS)[number];
export const PROFILE_LEVEL_LABELS: Record<ProfileLevel, string> = {
  vva: "V-VA (VAH · VAL · VPOC · VWAP)",
  tva: "T-VA (TPO-VAH · TPO-VAL · TPOC · TWAP)",
  hvn: "HVN (high volume nodes)",
  ibext: "IB extensions (1.5× · 2× · 3×)",
  dpoc: "Developing POC trail",
  weak: "Weak high / low",
  prev: "Previous day (POC · VA · high · low)",
};

export const LEVEL_COLORS = {
  poc: "#ff5fa8",                       // VPOC / TPOC: pink
  avg: "#22c55e",                       // VWAP / TWAP: green
  hvn: "#9a6a3a",                       // HVN: brown
  hvnFill: "rgba(154,106,58,0.30)",
  vaV: "rgba(76,141,255,0.10)", vaT: "rgba(160,120,255,0.08)", vaEdge: "#4c8dff", vaEdgeT: "#a078ff",
  ib: "#4c8dff", prev: "#8a93a3", weak: "#e0a43a",
};

export type LineStyle = "band" | "thick" | "solid" | "dashed" | "dotted";

export interface LevelLine {
  label: string;
  price: number;
  exact: boolean;          // an exact price (VWAP), drawn where it falls inside its row; else a row
  style: LineStyle;
  color: string;
}

export interface Shading {
  low: number;
  high: number;
  fill: string;
  label?: string;
  edge?: string;
}

export interface ProfileDrawing {
  shading: Shading[];      // value areas and HVN bands, drawn under the letters
  lines: LevelLine[];      // drawn over the letters
  trail: { period: number; letter: string; price: number }[];
  weak: ("high" | "low")[];
}

export interface LevelOptions {
  enabled: readonly ProfileLevel[];
  showPoc: boolean;        // the toolbar's POC button
  showValueArea: boolean;  // the toolbar's VA button
  previous: boolean;       // previous-day lines (single-day view)
}

export function profileDrawing(p: MarketProfile, o: LevelOptions): ProfileDrawing {
  const on = (k: ProfileLevel) => o.enabled.includes(k);
  const out: ProfileDrawing = { shading: [], lines: [], trail: [], weak: [] };
  const line = (label: string, price: number | null | undefined, style: LineStyle, color: string, exact = false) => {
    if (price != null) out.lines.push({ label, price, style, color, exact });
  };
  if (on("vva")) {
    const va = p.value_area_volume;
    if (va && o.showValueArea) out.shading.push({ low: va.low, high: va.high, fill: LEVEL_COLORS.vaV });
    if (va) {
      line("VAH", va.high, "solid", LEVEL_COLORS.vaEdge);
      line("VAL", va.low, "solid", LEVEL_COLORS.vaEdge);
    }
    if (o.showPoc) line("VPOC", p.poc_volume, "band", LEVEL_COLORS.poc);
    line("VWAP", p.vwap, "thick", LEVEL_COLORS.avg, true);
  }
  if (on("tva")) {
    const va = p.value_area_tpo;
    if (va && o.showValueArea) out.shading.push({ low: va.low, high: va.high, fill: LEVEL_COLORS.vaT });
    if (va) {
      line("TPO-VAH", va.high, "dashed", LEVEL_COLORS.vaEdgeT);
      line("TPO-VAL", va.low, "dashed", LEVEL_COLORS.vaEdgeT);
    }
    if (o.showPoc) line("TPOC", p.poc_tpo, "band", LEVEL_COLORS.poc);
    line("TWAP", p.twap, "thick", LEVEL_COLORS.avg);
  }
  if (on("hvn")) {
    for (const b of p.hvn_bands ?? []) {
      out.shading.push({ low: b.low, high: b.high, fill: LEVEL_COLORS.hvnFill, edge: LEVEL_COLORS.hvn, label: "HVN" });
    }
  }
  if (on("ibext") && p.ib_extensions) {
    for (const e of p.ib_extensions.up) line(`${e.multiple}× IB`, e.price, "dashed", LEVEL_COLORS.ib);
    for (const e of p.ib_extensions.down) line(`${e.multiple}× IB`, e.price, "dashed", LEVEL_COLORS.ib);
  }
  if (on("dpoc")) out.trail = p.dpoc?.trail ?? [];
  if (on("weak")) {
    if (p.weak_high) out.weak.push("high");
    if (p.weak_low) out.weak.push("low");
  }
  if (on("prev") && o.previous && p.previous) {
    const pv = p.previous;
    line("pPOC", pv.poc_tpo, "dotted", LEVEL_COLORS.prev);
    line("pVAH", pv.vah, "dotted", LEVEL_COLORS.prev);
    line("pVAL", pv.val, "dotted", LEVEL_COLORS.prev);
    line("PDH", pv.high, "dotted", LEVEL_COLORS.prev);
    line("PDL", pv.low, "dotted", LEVEL_COLORS.prev);
  }
  return out;
}

/** The levels on a row (for the hover text): exact prices count for the row they fall in. */
export function levelsOnRow(d: ProfileDrawing, rowPrice: number, rowSize: number): string[] {
  const inRow = (price: number, exact: boolean) =>
    exact ? price >= rowPrice - 1e-9 && price < rowPrice + rowSize - 1e-9 : Math.abs(price - rowPrice) < 1e-9;
  const names = d.lines.filter((l) => inRow(l.price, l.exact)).map((l) => l.label);
  for (const s of d.shading) if (s.label && rowPrice >= s.low - 1e-9 && rowPrice <= s.high + 1e-9) names.push(s.label);
  return [...new Set(names)];
}
