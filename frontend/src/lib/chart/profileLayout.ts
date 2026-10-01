// Market profile chart geometry: pure functions (no DOM), unit tested.
//
//   | TPO letters (grow right from the left edge) |    | volume at price | price axis |
//   (the volume histogram takes at most VOLUME_SHARE of the plot, against the axis)
//
// Rows are drawn high to low, one per profile row. When every row fits, the
// profile is shown whole; otherwise rows get a minimum height and the view
// scrolls (scrollRows = rows hidden above the top edge).
import type { MarketProfile, ProfileRow } from "../types";

export const AXIS_WIDTH = 64;
export const MIN_ROW_PX = 4;
export const MAX_ROW_PX = 22;
export const LETTER_MIN_PX = 7;            // below this font size, TPOs are drawn as blocks
export const VOLUME_SHARE = 0.35;
export const SPLIT_FOOTER_PX = 16;          // period letters / start times under a split profile

export type ProfileMode = "stacked" | "split";

export interface ProfileLayout {
  width: number;
  height: number;
  rowPx: number;
  top: number;                              // y of the first row (centres a profile shorter than the view)
  visibleRows: number;
  maxScroll: number;
  letterX: number;                          // TPO area: [letterX, letterX + letterW)
  letterW: number;
  cellW: number;                            // width of one TPO letter / block
  volumeX: number;                          // volume area: [volumeX, volumeX + volumeW)
  volumeW: number;
  axisX: number;
  fontPx: number;
  drawLetters: boolean;
  maxVolume: number;
  maxLetters: number;
  mode: ProfileMode;
  columns: number;                          // stacked: most letters in a row; split: number of periods
  footer: number;                           // px reserved at the bottom (split: period labels)
}

export function computeLayout(profile: MarketProfile, width: number, height: number,
                              mode: ProfileMode = "stacked"): ProfileLayout {
  const rows = profile.rows;
  const n = Math.max(1, rows.length);
  const plotW = Math.max(40, width - AXIS_WIDTH);
  const footer = mode === "split" ? SPLIT_FOOTER_PX : 0;
  const plotH = Math.max(20, height - footer);
  const rowPx = Math.min(MAX_ROW_PX, Math.max(MIN_ROW_PX, plotH / n));
  const visibleRows = Math.max(1, Math.floor(plotH / rowPx));
  const maxLetters = rows.reduce((m, r) => Math.max(m, r.letters.length), 1);
  const columns = mode === "split" ? splitColumns(profile) : maxLetters;
  const maxVolume = rows.reduce((m, r) => Math.max(m, r.volume), 0);
  const letterX = 10;                                   // room for the IB / single-print markers
  const volumeW = Math.max(10, Math.floor(plotW * VOLUME_SHARE));
  const volumeX = plotW - volumeW - 4;
  const letterW = Math.max(20, volumeX - 8 - letterX);
  const cellW = Math.max(2, Math.min(mode === "split" ? 28 : 14, letterW / columns));
  const fontPx = Math.floor(Math.min(rowPx * 0.85, cellW * 1.15, 13));
  return {
    width, height, rowPx, top: Math.max(0, Math.floor((plotH - n * rowPx) / 2)), visibleRows,
    maxScroll: Math.max(0, n - visibleRows),
    letterX, letterW, cellW, volumeX, volumeW, axisX: plotW,
    fontPx, drawLetters: fontPx >= LETTER_MIN_PX, maxVolume, maxLetters, mode, columns, footer,
  };
}

/** Split view: one column per period, from A up to the latest period that traded. */
export function splitColumns(profile: MarketProfile): number {
  const periods = profile.periods ?? [];
  return Math.max(1, periods.reduce((m, p) => Math.max(m, p.index + 1), 0));
}

/** x of a TPO: stacked packs a row's letters to the left; split puts each period in its own column. */
export function tpoX(layout: ProfileLayout, positionInRow: number, letter: string): number {
  const column = layout.mode === "split" ? letterIndex(letter) : positionInRow;
  return layout.letterX + column * layout.cellW;
}

/** Row index (0 = highest row) whose band contains `price`, or -1 if outside. */
export function rowIndexOf(rows: ProfileRow[], price: number, rowSize: number): number {
  if (!rows.length) return -1;
  const top = rows[0].price;
  const i = Math.round((top - Math.floor(price / rowSize + 1e-9) * rowSize) / rowSize);
  return i >= 0 && i < rows.length ? i : -1;
}

/** Scroll that centres `index` (clamped). */
export function centreOn(index: number, layout: ProfileLayout): number {
  return Math.max(0, Math.min(layout.maxScroll, Math.round(index - layout.visibleRows / 2)));
}

/** Price-axis labels: every k-th row so labels stay at least `minGapPx` apart. */
export function labelEvery(rowPx: number, minGapPx = 16): number {
  return Math.max(1, Math.ceil(minGapPx / rowPx));
}

/** A stable colour per period letter (A, B, C ...) cycling through hues. */
export function periodColor(index: number): string {
  const hue = (200 + index * 37) % 360;
  return `hsl(${hue} 62% 62%)`;
}

export function letterIndex(letter: string): number {
  const i = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz".indexOf(letter);
  return i < 0 ? 0 : i;
}
