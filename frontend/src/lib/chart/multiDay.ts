// Market profile chart model: one or more independent daily profiles side by
// side on ONE absolute price axis, with a pan/zoom viewport. Pure functions
// (no DOM), unit tested. The single-day chart is the same chart with one column.
//
//   columns     finished sessions (static) + today / the replayed session (live), oldest first
//   price grid  the union of every column's rows on the engine's absolute row grid
//               (row = floor(price / rowSize)); no day's own range decides the axis
//   viewport    topPrice (price at the plot's top edge, continuous), rowPx (Y zoom),
//               leftX (content px hidden left), colScale (X zoom), follow (track today)
//   layout      pixel geometry for a viewport (column width = base width x colScale)
//   mapper      priceToY / yToPrice / sessionToX / xToSession: the only place coordinates
//               are computed; drawing and hit testing share it
//
// Zoom changes pixels, never data: rowPx is pixels per row, the row size (points per
// row) stays the user's data setting. All days pan and zoom together, so a price is
// always at the same height in every column.
import type { MarketProfile, ProfileHistory, ProfileRow } from "../types";

export type ProfileMode = "stacked" | "split";

export interface DayColumn {
  date: string;
  state: "historical" | "live" | "error";
  profile: MarketProfile | null;
  error?: string;
  replay?: boolean;          // the live column is a replayed session, not today
}

export const HEADER_PX = 34;
export const AXIS_PX = 64;
export const FOOTER_SPLIT_PX = 14;
export const ROW_PX = { min: 2, max: 40, fitMin: 4, fitMax: 22 };
export const COL_SCALE = { min: 0.35, max: 3 };
export const VOLUME_MIN_PX = 30;          // narrower than this: volume bars give way to a vPOC tick
export const LETTER_MIN_PX = 7;           // smaller than this: TPOs are blocks, not letters
const EPS = 1e-9;

// -- data model ------------------------------------------------------------------------------

/** Finished sessions (oldest first) then the live column. History at or after the live date is
 *  dropped (stale after a session rollover); an empty live profile still gets its column. */
export function collectColumns(history: ProfileHistory | undefined, live: MarketProfile | null,
                               replay = false): DayColumn[] {
  const liveDate = live?.date ?? history?.live_date ?? null;
  const past = replay ? [] : (history?.sessions ?? [])
    .filter((s) => liveDate === null || s.date < liveDate)
    .sort((a, b) => a.date.localeCompare(b.date))
    .map<DayColumn>((s) => ({ date: s.date, state: s.state, profile: s.profile ?? null, error: s.error }));
  return [...past, { date: liveDate ?? "today", state: "live", profile: live && live.rows.length ? live : null,
                     replay }];
}

/** Absolute row index of a price on the engine's grid (rows are labelled by their floor). */
export const rowOf = (price: number, rowSize: number) => Math.floor(price / rowSize + EPS);
const rowFloor = (price: number, rowSize: number) => Math.round(rowOf(price, rowSize) * rowSize * 1e8) / 1e8;

export interface PriceGrid {
  rowSize: number;
  topRow: number;
  bottomRow: number;
  rows: number;
  high: number;            // top edge of the highest row (price)
  low: number;             // bottom edge of the lowest row (price)
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
  return { rowSize, topRow: top, bottomRow: bottom, rows: top - bottom + 1,
           high: (top + 1) * rowSize, low: bottom * rowSize };
}

// -- layout ----------------------------------------------------------------------------------

export interface Viewport {
  topPrice: number;        // price at the plot's top edge
  rowPx: number;           // Y zoom: pixels per row
  leftX: number;           // content px hidden on the left (negative: content right-aligned in a wider view)
  colScale: number;        // X zoom: column width multiplier
  follow: boolean;         // keep today in view with its last price centred
}

export interface MultiLayout {
  width: number;
  height: number;
  axisX: number;           // the price axis starts here (fixed while sessions pan)
  plotTop: number;
  plotH: number;
  footer: number;
  mode: ProfileMode;
  tpoCols: number;         // letter columns per day (stacked: most letters in a row; split: periods)
  baseColW: number;        // column width at colScale 1
  rowPx: number;
  cellW: number;
  tpoW: number;
  volumeW: number;
  showVolume: boolean;
  pad: number;             // left padding inside a column (IB bracket, single-print marks)
  gap: number;             // between the TPO area and the volume bars
  colW: number;
  contentW: number;
  fontPx: number;
  drawLetters: boolean;
  thinBars: boolean;       // extreme zoom-out: one bar per row instead of cells
}

const BASE = { stackedCell: 10, splitCell: 12, pad: 14, gap: 6, right: 10, volMin: 40, volMax: 90 };

function tpoColsOf(columns: DayColumn[], mode: ProfileMode): number {
  let n = 1;
  for (const c of columns) {
    const p = c.profile;
    if (!p) continue;
    n = Math.max(n, mode === "split" ? (p.periods ?? []).reduce((m, x) => Math.max(m, x.index + 1), 1)
                                      : p.rows.reduce((m, r) => Math.max(m, r.letters.length), 1));
  }
  return n;
}

export function computeLayout(columns: DayColumn[], width: number, height: number, mode: ProfileMode,
                              vp: Pick<Viewport, "rowPx" | "colScale">): MultiLayout {
  const footer = mode === "split" ? FOOTER_SPLIT_PX : 0;
  const tpoCols = tpoColsOf(columns, mode);
  const baseCell = mode === "split" ? BASE.splitCell : BASE.stackedCell;
  const baseVol = Math.max(BASE.volMin, Math.min(BASE.volMax, Math.round(tpoCols * baseCell * 0.6)));
  const baseColW = BASE.pad + tpoCols * baseCell + BASE.gap + baseVol + BASE.right;
  const s = vp.colScale;
  const cellW = baseCell * s, tpoW = tpoCols * cellW, volumeW = baseVol * s;
  const pad = BASE.pad * s;                     // every part scales: column width stays proportional to colScale
  const colW = pad + tpoW + BASE.gap * s + volumeW + BASE.right * s;
  const fontPx = Math.floor(Math.min(vp.rowPx * 0.85, cellW * 1.15, 14));
  return {
    width, height, axisX: Math.max(40, width - AXIS_PX), plotTop: HEADER_PX,
    plotH: Math.max(20, height - HEADER_PX - footer), footer, mode, tpoCols, baseColW, rowPx: vp.rowPx,
    cellW, tpoW, volumeW, showVolume: volumeW >= VOLUME_MIN_PX, pad, gap: BASE.gap * s, colW,
    contentW: columns.length * colW,
    fontPx, drawLetters: fontPx >= LETTER_MIN_PX && cellW >= 6, thinBars: cellW < 4,
  };
}

// -- viewport operations (pure) ---------------------------------------------------------------

const clampN = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v));
/** Price span the plot shows at this viewport. */
export const visibleSpan = (l: MultiLayout, rowSize: number) => (l.plotH / l.rowPx) * rowSize;

/** Keep the view on the data: at most half a screen of empty space past either end, content
 *  centred vertically when it is shorter than the view, right-aligned (today by the axis)
 *  when it is narrower. */
export function clampViewport(vp: Viewport, l: MultiLayout, grid: PriceGrid): Viewport {
  const span = visibleSpan(l, grid.rowSize);
  const dataSpan = grid.high - grid.low;
  const topPrice = dataSpan <= span
    ? (grid.high + grid.low) / 2 + span / 2
    : clampN(vp.topPrice, grid.low + span / 2, grid.high + span / 2);
  const viewW = l.axisX;
  const leftX = l.contentW <= viewW ? l.contentW - viewW
    : clampN(vp.leftX, -viewW / 2, l.contentW - viewW / 2);
  return { ...vp, topPrice, leftX };
}

/** Zoom levels that fit: rows fit the plot height (within the usual 4-22 px), columns fill the
 *  width when there are few (never shrunk below 1x automatically; more days scroll instead). */
export function fitZoom(columns: DayColumn[], grid: PriceGrid, width: number, height: number,
                        mode: ProfileMode): Pick<Viewport, "rowPx" | "colScale"> {
  const probe = computeLayout(columns, width, height, mode, { rowPx: ROW_PX.fitMax, colScale: 1 });
  const rowPx = clampN(probe.plotH / grid.rows, ROW_PX.fitMin, ROW_PX.fitMax);
  const colScale = clampN(probe.axisX / (columns.length * probe.baseColW), 1, COL_SCALE.max);
  return { rowPx, colScale };
}

/** Follow today: rightmost column in view, the last price centred. */
export function followView(vp: Viewport, l: MultiLayout, grid: PriceGrid, lastPrice: number | null | undefined): Viewport {
  const span = visibleSpan(l, grid.rowSize);
  const centre = lastPrice ?? (grid.high + grid.low) / 2;
  return clampViewport({ ...vp, topPrice: centre + span / 2, leftX: l.contentW - l.axisX, follow: true }, l, grid);
}

/** Zoom the price axis by `factor`, keeping the price under `anchorY` where it is. */
export function zoomY(vp: Viewport, factor: number, anchorY: number, l: MultiLayout, rowSize: number): Viewport {
  const rowPx = clampN(vp.rowPx * factor, ROW_PX.min, ROW_PX.max);
  const dy = anchorY - l.plotTop;
  const anchorPrice = vp.topPrice - (dy / vp.rowPx) * rowSize;
  return { ...vp, rowPx, topPrice: anchorPrice + (dy / rowPx) * rowSize, follow: false };
}

/** Zoom the day axis by `factor`, keeping the content under `anchorX` where it is. */
export function zoomX(vp: Viewport, factor: number, anchorX: number): Viewport {
  const colScale = clampN(vp.colScale * factor, COL_SCALE.min, COL_SCALE.max);
  const ratio = colScale / vp.colScale;
  return { ...vp, colScale, leftX: (anchorX + vp.leftX) * ratio - anchorX, follow: false };
}

/** Pan by a pointer movement: content follows the pointer. */
export function panBy(vp: Viewport, dxPx: number, dyPx: number, l: MultiLayout, rowSize: number): Viewport {
  return { ...vp, leftX: vp.leftX - dxPx, topPrice: vp.topPrice + (dyPx / l.rowPx) * rowSize, follow: false };
}

// -- mapper ----------------------------------------------------------------------------------

export interface Mapper {
  priceToY: (price: number) => number;              // top edge of the price's row
  yToPrice: (y: number) => number | null;           // floor price of the row under y (inside the grid)
  sessionToX: (index: number) => number;            // left edge of a session column
  xToSession: (x: number) => number | null;
  tpoX: (index: number, positionInRow: number, letter: string) => number;
  visibleSessions: () => [number, number];          // [first, last] intersecting the view (first > last: none)
}

export function mapper(l: MultiLayout, grid: PriceGrid, vp: Viewport, columns: number): Mapper {
  const rs = grid.rowSize;
  const priceToY = (price: number) => l.plotTop + ((vp.topPrice - (rowFloor(price, rs) + rs)) / rs) * l.rowPx;
  const sessionToX = (i: number) => i * l.colW - vp.leftX;
  return {
    priceToY,
    yToPrice: (y) => {
      if (y < l.plotTop || y >= l.plotTop + l.plotH) return null;
      const price = vp.topPrice - ((y - l.plotTop) / l.rowPx) * rs;
      const row = rowOf(price, rs);
      return row < grid.bottomRow || row > grid.topRow ? null : rowFloor(price, rs);
    },
    sessionToX,
    xToSession: (x) => {
      if (x < 0 || x >= l.axisX) return null;
      const i = Math.floor((x + vp.leftX) / l.colW);
      return i >= 0 && i < columns ? i : null;
    },
    tpoX: (i, pos, letter) => {
      const col = l.mode === "split" ? LETTERS.indexOf(letter) : pos;
      return sessionToX(i) + l.pad + Math.max(0, col) * l.cellW;
    },
    visibleSessions: () => [Math.max(0, Math.floor(vp.leftX / l.colW)),
                            Math.min(columns - 1, Math.floor((vp.leftX + l.axisX - 1) / l.colW))],
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

/** Price-axis label step: a round number of points (a multiple of the row size) >= minGapPx apart. */
export function labelStep(rowPx: number, rowSize: number, minGapPx = 16): number {
  const steps = [1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000, 2500, 5000]
    .filter((s) => Math.abs(s / rowSize - Math.round(s / rowSize)) < EPS && s >= rowSize);
  return steps.find((s) => (s / rowSize) * rowPx >= minGapPx) ?? steps[steps.length - 1];
}

/** "07 Oct" (dates are ISO session dates). */
export function shortDate(iso: string): string {
  const d = new Date(`${iso}T00:00:00Z`);
  return Number.isNaN(d.getTime()) ? iso
    : d.toLocaleDateString("en-GB", { day: "2-digit", month: "short", timeZone: "UTC" });
}
