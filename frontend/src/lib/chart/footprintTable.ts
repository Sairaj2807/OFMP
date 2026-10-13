// Chart-library primitive: the footprint table, one column under each candle
// and one row per figure (owner decisions 2026-10-13):
//   Volume · VPOC · VPOC Vol · COT (buy − sell) · OI (closing) · dOI
// The server computes every value (domain/orderflow/chart.py), so live,
// replay and history agree; this only lays the numbers out. Values follow the
// Qty/Lots toggle; unknown values show "—", never 0.
import type { EngineBar } from "../types";

export const TABLE_ROWS = ["volume", "vpoc", "vpocVol", "cot", "oi", "doi"] as const;
export type TableRow = (typeof TABLE_ROWS)[number];
export const TABLE_ROW_LABELS: Record<TableRow, string> = {
  volume: "Volume", vpoc: "VPOC", vpocVol: "VPOC Vol", cot: "COT", oi: "OI", doi: "dOI",
};
export const ROW_PX = 16;
export const LABEL_PX = 62;
const COLUMN_FACTOR = 0.84;             // the footprint's own column width (widthFactor)

export const TABLE_COLORS = {
  bg: "#0f1217", line: "#252b35", label: "#7d8796", text: "#dfe4ec", buy: "#1fb58f", sell: "#e5485d",
  vpoc: "#ff5fa8",
};

interface RenderContext {
  dpr: number;
  plotWidth: number;
  plotHeight: number;
  dataLayer: { timeToIndex(time: number): number | undefined };
  timeScale: { indexToX(index: number): number; barSpacing: number };
}

/** A row's raw value for a bar (null: unknown). Quantities are in contracts' quantity (before units). */
export function rowValue(row: TableRow, b: EngineBar): number | null {
  switch (row) {
    case "volume": return b.volume ?? null;
    case "vpoc": return b.vpoc ?? null;
    case "vpocVol": return b.vpoc_volume ?? null;
    case "cot": return b.cot ?? null;
    case "oi": return b.oi ?? null;
    case "doi": return b.doi ?? null;
  }
}

const isQuantity = (row: TableRow) => row !== "vpoc";
const isSigned = (row: TableRow) => row === "cot" || row === "doi";

/** 12345 -> "12.3K", 18_420_000 -> "18.42M" (shorter forms for narrow columns). */
export function compact(v: number): string {
  const a = Math.abs(v);
  const sign = v < 0 ? "−" : "";
  if (a >= 1e7) return `${sign}${(a / 1e6).toFixed(1)}M`;
  if (a >= 1e6) return `${sign}${(a / 1e6).toFixed(2)}M`;
  if (a >= 1e4) return `${sign}${(a / 1e3).toFixed(1)}K`;
  if (a >= 1e3) return `${sign}${(a / 1e3).toFixed(2)}K`;
  return `${sign}${Number.isInteger(a) ? a : a.toFixed(2)}`;
}

/** The text forms of a value, longest first; the table uses the first that fits its column. */
export function cellTexts(row: TableRow, v: number | null, divisor: number): string[] {
  if (v === null) return ["—"];
  if (row === "vpoc") return [Number.isInteger(v) ? String(v) : v.toFixed(2)];
  const q = isQuantity(row) ? v / divisor : v;
  const plus = isSigned(row) && q > 0 ? "+" : "";
  const full = plus + q.toLocaleString("en-US", { maximumFractionDigits: 2 }).replace("-", "−");
  return [full, plus + compact(q)];
}

/** Text colour of a cell: COT and dOI by sign, VPOC pink (it marks the pink POC line). */
export function cellColor(row: TableRow, v: number | null): string {
  if (v === null) return TABLE_COLORS.label;
  if (row === "vpoc") return TABLE_COLORS.vpoc;
  if (isSigned(row)) return v > 0 ? TABLE_COLORS.buy : v < 0 ? TABLE_COLORS.sell : TABLE_COLORS.text;
  return TABLE_COLORS.text;
}

export const tableHeight = (rows: readonly TableRow[]) => (rows.length ? rows.length * ROW_PX + 2 : 0);

export class FootprintTable {
  private rows: TableRow[] = [...TABLE_ROWS];
  private visible = true;
  private divisor = 1;
  private bars = new Map<number, EngineBar>();
  private host: { requestUpdate(): void } | null = null;
  private rc: RenderContext | null = null;

  attached(host: { requestUpdate(): void }): void {
    this.host = host;
  }

  detached(): void {
    this.host = null;
    this.rc = null;
  }

  zOrder(): "top" {
    return "top";
  }

  autoscaleInfo(): null {
    return null;
  }

  /** Rows shown (in the table's fixed order); none or hidden: the table takes no space. */
  setOptions(o: { visible?: boolean; rows?: readonly TableRow[]; divisor?: number }): void {
    if (o.visible !== undefined) this.visible = o.visible;
    if (o.rows) this.rows = TABLE_ROWS.filter((r) => o.rows!.includes(r));
    if (o.divisor !== undefined) this.divisor = o.divisor;
    this.host?.requestUpdate();
  }

  setBars(bars: EngineBar[]): void {
    this.bars = new Map(bars.map((b) => [b.time, b]));
    this.host?.requestUpdate();
  }

  /** CSS pixels the table occupies at the bottom of the plot. */
  height(): number {
    return this.visible ? tableHeight(this.rows) : 0;
  }

  bar(time: number): EngineBar | undefined {
    return this.bars.get(time);
  }

  /** The bar time under a point inside the table, or null. */
  hitTest(x: number, y: number): number | null {
    const rc = this.rc;
    const h = this.height();
    if (!rc || !h || y < rc.plotHeight - h || y > rc.plotHeight || x < LABEL_PX) return null;
    const half = (rc.timeScale.barSpacing * COLUMN_FACTOR) / 2;
    for (const time of this.bars.keys()) {
      const i = rc.dataLayer.timeToIndex(time);
      if (i === undefined) continue;
      const cx = rc.timeScale.indexToX(i);
      if (x >= cx - half && x <= cx + half) return time;
    }
    return null;
  }

  draw(ctx: CanvasRenderingContext2D, rc: RenderContext): void {
    this.rc = rc;
    const h = this.height();
    if (!h) return;
    const r = rc.dpr;
    const top = rc.plotHeight - h;
    const W = rc.plotWidth;
    ctx.save();
    ctx.fillStyle = TABLE_COLORS.bg;
    ctx.fillRect(0, top * r, W * r, h * r);
    ctx.strokeStyle = TABLE_COLORS.line;
    ctx.lineWidth = Math.max(1, r);
    ctx.beginPath();
    for (let k = 0; k <= this.rows.length; k++) {
      const y = Math.round((top + 1 + k * ROW_PX) * r) + 0.5;
      ctx.moveTo(0, y);
      ctx.lineTo(W * r, y);
    }
    ctx.stroke();

    ctx.font = `${11 * r}px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    const colW = rc.timeScale.barSpacing * COLUMN_FACTOR;
    for (const [time, b] of this.bars) {
      const i = rc.dataLayer.timeToIndex(time);
      if (i === undefined) continue;
      const cx = rc.timeScale.indexToX(i);
      if (cx + colW / 2 < LABEL_PX || cx - colW / 2 > W) continue;
      this.rows.forEach((row, k) => {
        const v = rowValue(row, b);
        const text = cellTexts(row, v, this.divisor).find((t) => ctx.measureText(t).width / r <= colW - 6);
        const y = (top + 1 + k * ROW_PX + ROW_PX / 2) * r;
        ctx.fillStyle = cellColor(row, v);
        if (text) {
          ctx.fillText(text, cx * r, y);
        } else if (v !== null) {
          // too narrow for any text: a dot in the row's colour still shows sign / presence
          ctx.fillRect(cx * r - r, y - r, 2 * r, 2 * r);
        }
      });
    }

    // row labels, fixed on the left over the columns
    ctx.fillStyle = TABLE_COLORS.bg;
    ctx.fillRect(0, top * r, LABEL_PX * r, h * r);
    ctx.strokeStyle = TABLE_COLORS.line;
    ctx.beginPath();
    ctx.moveTo(Math.round(LABEL_PX * r) + 0.5, top * r);
    ctx.lineTo(Math.round(LABEL_PX * r) + 0.5, rc.plotHeight * r);
    ctx.stroke();
    ctx.textAlign = "left";
    this.rows.forEach((row, k) => {
      ctx.fillStyle = row === "vpoc" || row === "vpocVol" ? TABLE_COLORS.vpoc : TABLE_COLORS.label;
      ctx.fillText(TABLE_ROW_LABELS[row], 6 * r, (top + 1 + k * ROW_PX + ROW_PX / 2) * r);
    });
    ctx.restore();
  }
}
