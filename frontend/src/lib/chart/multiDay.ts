// Multi-day market profile: independent daily profiles side by side on ONE
// absolute price axis. Pure functions (no DOM), unit tested.
//
//   data model      columns: finished sessions (static) + today (live), oldest first
//   price grid      the union of every column's rows, on the engine's absolute
//                   row grid (row = floor(price / rowSize)); no day's own range
//                   decides the axis
//   layout          one fixed-width column per session (TPO area | volume area),
//                   a header row of dates, one price axis on the right
//   viewport        scrollX in pixels (sessions), scrollY in rows (price)
//   mapper          priceToY / yToPrice / sessionToX / xToSession: the only
//                   place coordinates are computed (drawing and hit testing share it)
import type { MarketProfile, ProfileHistory, ProfileRow } from "../types";

export type ProfileMode = "stacked" | "split";

export interface DayColumn {
  date: string;
  state: "historical" | "live" | "error";
  profile: MarketProfile | null;
  error?: string;
}

export const HEADER_PX = 34;
export const AXIS_PX = 64;
export const FOOTER_SPLIT_PX = 14;
export const MIN_ROW_PX = 4;
export const MAX_ROW_PX = 22;
const EPS = 1e-9;

/** Finished sessions (oldest first) then today. History at or after today's date is dropped
 *  (stale after a session rollover); an empty live profile still gets its column. */
export function collectColumns(history: ProfileHistory | undefined, live: MarketProfile | null): DayColumn[] {
  const liveDate = live?.date ?? history?.live_date ?? null;
  const past = (history?.sessions ?? [])
    .filter((s) => liveDate === null || s.date < liveDate)
    .sort((a, b) => a.date.localeCompare(b.date))
    .map<DayColumn>((s) => ({ date: s.date, state: s.state, profile: s.profile ?? null, error: s.error }));
  return [...past, { date: liveDate ?? "today", state: "live", profile: live && live.rows.length ? live : null }];
}

/** Absolute row index of a price on the engine's grid (rows are labelled by their floor). */
export const rowOf = (price: number, rowSize: number) => Math.floor(price / rowSize + EPS);

export interface PriceGrid {
  rowSize: number;
  topRow: number;          // highest absolute row index shown
  bottomRow: number;
  rows: number;
}

/** The shared price range: the union of every column's rows. */
export function priceGrid(columns: DayColumn[], rowSize: number): PriceGrid | null {
  let top = -Infinity, bottom = Infinity;
  for (const c of columns) {
    const rows = c.profile?.rows;
    if (!rows?.length) continue;
    top = Math.max(top, rowOf(rows[0].price, rowSize));
    bottom = Math.min(bottom, rowOf(rows[rows.length - 1].price, rowSize));
  }
  if (!Number.isFinite(top)) return null;
  return { rowSize, topRow: top, bottomRow: bottom, rows: top - bottom + 1 };
}

export interface MultiLayout {
  width: number;
  height: number;
  axisX: number;            // price axis starts here (fixed while scrolling sessions)
  plotTop: number;          // below the header
  plotH: number;
  footer: number;
  rowPx: number;
  top: number;              // offset centring a short price range
  visibleRows: number;
  maxScrollY: number;
  mode: ProfileMode;
  tpoCols: number;          // letter columns per day (stacked: most letters; split: periods)
  cellW: number;
  tpoW: number;
  volumeW: number;
  colW: number;             // one session column
  contentW: number;
  maxScrollX: number;
  fontPx: number;
  drawLetters: boolean;
}

export function computeMultiLayout(columns: DayColumn[], grid: PriceGrid, width: number, height: number,
                                   mode: ProfileMode): MultiLayout {
  const footer = mode === "split" ? FOOTER_SPLIT_PX : 0;
  const plotTop = HEADER_PX;
  const plotH = Math.max(20, height - HEADER_PX - footer);
  const rowPx = Math.min(MAX_ROW_PX, Math.max(MIN_ROW_PX, plotH / grid.rows));
  const visibleRows = Math.max(1, Math.floor(plotH / rowPx));
  let tpoCols = 1;
  for (const c of columns) {
    const p = c.profile;
    if (!p) continue;
    tpoCols = Math.max(tpoCols, mode === "split"
      ? (p.periods ?? []).reduce((m, x) => Math.max(m, x.index + 1), 1)
      : p.rows.reduce((m, r) => Math.max(m, r.letters.length), 1));
  }
  const cellW = mode === "split" ? 12 : 10;
  const tpoW = tpoCols * cellW;
  const volumeW = Math.max(40, Math.min(90, Math.round(tpoW * 0.6)));
  const colW = 14 + tpoW + 6 + volumeW + 10;                  // pad | TPO | gap | volume | pad
  const axisX = Math.max(40, width - AXIS_PX);
  const contentW = columns.length * colW;
  const fontPx = Math.floor(Math.min(rowPx * 0.85, cellW * 1.15, 13));
  return {
    width, height, axisX, plotTop, plotH, footer, rowPx,
    top: Math.max(0, Math.floor((plotH - grid.rows * rowPx) / 2)), visibleRows,
    maxScrollY: Math.max(0, grid.rows - visibleRows), mode, tpoCols, cellW, tpoW, volumeW, colW, contentW,
    maxScrollX: Math.max(0, contentW - axisX), fontPx, drawLetters: fontPx >= 7,
  };
}

export interface Scroll {
  x: number;                // px of session columns hidden left
  y: number;                // rows hidden above
}

export function clampScroll(s: Scroll, l: MultiLayout): Scroll {
  return { x: Math.max(0, Math.min(l.maxScrollX, s.x)), y: Math.max(0, Math.min(l.maxScrollY, s.y)) };
}

/** Following the market: today's column in view (rightmost), its last price centred vertically. */
export function followScroll(l: MultiLayout, grid: PriceGrid, lastPrice: number | null | undefined): Scroll {
  const y = lastPrice == null ? 0 : Math.round(grid.topRow - rowOf(lastPrice, grid.rowSize) - l.visibleRows / 2);
  return clampScroll({ x: l.maxScrollX, y }, l);
}

export interface Mapper {
  priceToY: (price: number) => number;            // top edge of the price's row
  yToPrice: (y: number) => number | null;         // floor price of the row under y
  sessionToX: (index: number) => number;          // left edge of a session column
  xToSession: (x: number) => number | null;
  tpoX: (index: number, positionInRow: number, letter: string) => number;
  visibleSessions: () => [number, number];        // [first, last] column indexes intersecting the view
}

export function mapper(l: MultiLayout, grid: PriceGrid, s: Scroll, columns: number): Mapper {
  const y0 = l.plotTop + l.top;
  const priceToY = (price: number) => y0 + (grid.topRow - rowOf(price, grid.rowSize) - s.y) * l.rowPx;
  const sessionToX = (i: number) => i * l.colW - s.x;
  return {
    priceToY,
    yToPrice: (y) => {
      if (y < y0 || y >= l.plotTop + l.plotH) return null;
      const row = grid.topRow - Math.floor((y - y0) / l.rowPx) - s.y;
      return row < grid.bottomRow || row > grid.topRow ? null : Math.round(row * grid.rowSize * 1e8) / 1e8;
    },
    sessionToX,
    xToSession: (x) => {
      if (x < 0 || x >= l.axisX) return null;
      const i = Math.floor((x + s.x) / l.colW);
      return i >= 0 && i < columns ? i : null;
    },
    tpoX: (i, pos, letter) => {
      const col = l.mode === "split" ? LETTERS.indexOf(letter) : pos;
      return sessionToX(i) + 14 + Math.max(0, col) * l.cellW;
    },
    visibleSessions: () => [Math.max(0, Math.floor(s.x / l.colW)),
                            Math.min(columns - 1, Math.floor((s.x + l.axisX - 1) / l.colW))],
  };
}

const LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz";

/** The row of `column` at `price` (its own row, never another day's). */
export function rowAt(column: DayColumn, price: number, rowSize: number): ProfileRow | null {
  const rows = column.profile?.rows;
  if (!rows?.length) return null;
  const i = rowOf(rows[0].price, rowSize) - rowOf(price, rowSize);
  return i >= 0 && i < rows.length ? rows[i] : null;
}

/** "07 Oct" (dates are ISO session dates). */
export function shortDate(iso: string): string {
  const d = new Date(`${iso}T00:00:00Z`);
  return Number.isNaN(d.getTime()) ? iso
    : d.toLocaleDateString("en-GB", { day: "2-digit", month: "short", timeZone: "UTC" });
}
