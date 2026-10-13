// Imperative footprint chart controller: owns the chart-library objects and
// applies snapshots to them directly. Kept out of React so that a 2 Hz stream
// never re-renders components; FootprintChart only creates it, forwards
// setting changes to it, and destroys it. Behaviour ported from
// static/orderflow.js (autoscale on visible columns, follow the right edge,
// keep the inspected columns in place while data updates).
//
// Multi-day (applySessions): one footprint layer per session on one time axis,
// each with its own CVD start, so CVD restarts at every session; a marks layer
// draws the session separators and dates. Earlier sessions are prepended
// without moving the columns in view.
import type { Chart, Series } from "@/vendor/openalgo-charts/openalgo-charts.mjs";
import type { Footprint, FootprintBar, FootprintHover } from "@/vendor/openalgo-charts/openalgo-charts.profile.mjs";

import { bandRange, hasOhlc, toCandle, toFootprintBar } from "../adapter";
import { istTime } from "../format";
import type { ChartSnapshot, EngineBar } from "../types";
import { type FootprintSession, sessionStarts } from "./footprintSessions";
import { FootprintTable, rowValue, TABLE_ROW_LABELS, cellTexts, type TableRow } from "./footprintTable";
import { SessionMarks } from "./sessionMarks";

export const COLORS = {
  bg: "#12151b", grid: "#181c23", border: "#252b35", text: "#a3acba",
  buy: "#1fb58f", sell: "#e5485d", poc: "#ff5fa8", va: "#5d6b80", cell: "#eef3fa",   // POC = VPOC: pink
};
const TRANSPARENT = "rgba(0,0,0,0)";
const MIN_COLUMN_PX = 125;
const CARD_ROWS = ["volume", "delta", "deltaPct"] as const;   // the per-candle card, when the table is off
const CARD_MARGIN = 0.24;                                       // price-scale bottom margin the card needs
const TIME_AXIS_PX = 28;
/** Ask for an earlier session when fewer than this many columns are left of the view. */
export const LOAD_MORE_COLUMNS = 20;

interface Group {
  key: string;            // session date ("" in single-day mode)
  bars: EngineBar[];
  cvdOffset: number;
  live?: boolean;
}

export interface DisplayOptions {
  cellStyle: string;
  displayMode: string;
  showPoc: boolean;
  showValueArea: boolean;
  showImbalances: boolean;
}

export interface UnitOptions {
  units: "qty" | "lots";
  lotSize: number;
  tickSize: number;
}

export interface BarStats {
  delta: number;
  cvd: number;
  volume: number;
}

type ChartModule = typeof import("@/vendor/openalgo-charts/openalgo-charts.mjs");
type ProfileModule = typeof import("@/vendor/openalgo-charts/openalgo-charts.profile.mjs");

export class FootprintController {
  private readonly chart: Chart;
  private readonly footprint: Footprint;       // the newest session's layer; drives autoscale
  private readonly layers = new Map<string, Footprint>();   // earlier sessions, by date
  private readonly marks = new SessionMarks();
  private readonly table = new FootprintTable();
  private height: number;
  private readonly series: Series;
  private readonly newFootprint: () => Footprint;
  private multi = false;
  /** Multi-day mode: called after every pan or redraw with whether earlier sessions are nearly in view. */
  onViewport: (nearStart: boolean) => void = () => {};
  private dateOf = new Map<number, string>();
  private readonly observer: ResizeObserver;
  private bars: FootprintBar[] = [];
  private prevTimes: number[] = [];
  private rowSize = 1;
  private forceFit = true;
  private width: number;
  private units: UnitOptions;

  constructor(
    private readonly container: HTMLElement,
    private readonly tooltip: HTMLElement,
    private readonly empty: HTMLElement,
    libs: { chart: ChartModule; profile: ProfileModule },
    display: DisplayOptions,
    units: UnitOptions,
    private readonly onStats: (stats: BarStats | null) => void,
  ) {
    this.units = units;
    const { createChart, darkTheme } = libs.chart;
    this.chart = createChart(container, {
      theme: darkTheme, priceAxisWidth: 70, priceFormatter: (v: number) => v.toFixed(2),
      branding: false, // platform branding; the library's license/NOTICE ship in src/vendor/openalgo-charts/
      timeScale: { maxBarSpacing: 260 },
      priceScale: { minMove: 0.1, marginTop: 0.08, marginBottom: 0.24 },
    });
    this.chart.setTheme({
      ...darkTheme, background: COLORS.bg, grid: COLORS.grid, axisLine: COLORS.border, axisText: COLORS.text,
      buy: COLORS.buy, sell: COLORS.sell, upColor: COLORS.buy, downColor: COLORS.sell,
      wickUpColor: COLORS.buy, wickDownColor: COLORS.sell, lastPriceUp: COLORS.buy, lastPriceDown: COLORS.sell,
      lastPriceText: COLORS.bg,
    });
    // real OHLC drives autoscale and the time axis; the footprint draws its own candle
    this.series = this.chart.addSeries("candlestick", {
      style: { upColor: TRANSPARENT, downColor: TRANSPARENT, wickUpColor: TRANSPARENT, wickDownColor: TRANSPARENT,
               borderUpColor: TRANSPARENT, borderDownColor: TRANSPARENT },
    });
    this.newFootprint = () => new libs.profile.Footprint({
      textColorMode: "contrast", statsPosition: "bar", statsRows: ["volume", "delta", "deltaPct"], tableRows: [],
      pocStyle: "outline", valueAreaPercent: 0.7, imbalanceRatio: 3,
      widthFactor: 0.84, font: 11, radius: 1, statsRowHeight: 18, volumeDivisor: this.divisor(),
      buyColor: COLORS.buy, sellColor: COLORS.sell, pocColor: COLORS.poc, valueAreaColor: COLORS.va,
      textColor: COLORS.cell, buyTextColor: "#8aefcd", sellTextColor: "#ffb2bd",
      ...FootprintController.displayOptions(display),
    });
    this.display = display;
    this.footprint = this.newFootprint();
    this.chart.addPrimitive(this.footprint);
    this.chart.addPrimitive(this.marks);
    this.chart.addPrimitive(this.table);
    this.table.setOptions({ divisor: this.divisor() });

    // Autoscale on the columns in (or next to) view, not every loaded column.
    const loadedExtent = this.footprint.autoscaleInfo.bind(this.footprint);
    this.footprint.autoscaleInfo = () => this.visibleExtent() ?? loadedExtent();

    this.chart.on("crosshair:move", (e) => {
      const time = e.point ? this.table.hitTest(e.point.x, e.point.y) : null;
      if (time !== null && e.point) this.renderTableTooltip(time, e.point);
      else this.renderTooltip(e.point ? this.hoverAt(e.point.x, e.point.y) : null, e.point);
    });

    // The library has no visible-range event: look after every gesture that can pan.
    const check = () => setTimeout(() => this.checkNearStart(), 0);
    for (const ev of ["wheel", "pointerup", "keyup"]) container.addEventListener(ev, check, { passive: true });

    this.width = container.clientWidth;
    this.height = container.clientHeight;
    this.observer = new ResizeObserver(() => {
      if (container.clientHeight !== this.height) {
        this.height = container.clientHeight;
        this.fitPriceArea();
      }
      if (container.clientWidth !== this.width) {
        this.width = container.clientWidth;
        this.fitColumns();
      }
    });
    this.observer.observe(container);
  }

  private static displayOptions(d: DisplayOptions) {
    return { cellStyle: d.cellStyle, displayMode: d.displayMode, showPoc: d.showPoc, showValueArea: d.showValueArea,
             stackedImbalances: d.showImbalances ? 3 : 0 };
  }

  private display: DisplayOptions;

  private allLayers(): Footprint[] {
    return [...this.layers.values(), this.footprint];
  }

  private hoverAt(x: number, y: number): FootprintHover | null {
    for (const fp of this.allLayers()) {
      const hit = fp.hoverAt(x, y);
      if (hit) return hit;
    }
    return null;
  }

  private divisor(): number {
    return this.units.units === "lots" ? this.units.lotSize : 1;
  }

  setDisplay(display: DisplayOptions): void {
    this.display = display;
    for (const fp of this.allLayers()) fp.setOptions(FootprintController.displayOptions(display));
  }

  setUnits(units: UnitOptions): void {
    this.units = units;
    for (const fp of this.allLayers()) fp.setOptions({ volumeDivisor: this.divisor() });
    this.table.setOptions({ divisor: this.divisor() });
  }

  /** The footprint table: shown or not, and which rows. It replaces the per-candle card. */
  setTable(visible: boolean, rows: readonly TableRow[]): void {
    this.table.setOptions({ visible, rows });
    for (const fp of this.allLayers()) fp.setOptions({ statsRows: this.cardRows() });
    this.fitPriceArea();
  }

  private cardRows(): string[] {
    return this.table.height() ? [] : [...CARD_ROWS];
  }

  /** Keep candles above the table: the price scale's bottom margin is the table's height. */
  private fitPriceArea(): void {
    const plot = Math.max(1, this.container.clientHeight - TIME_AXIS_PX);
    const h = this.table.height();
    const marginBottom = h ? Math.min(0.45, (h + 14) / plot) : CARD_MARGIN;
    this.chart.setPriceScaleOptions({ marginBottom });
  }

  /** Refit the columns on the next snapshot (e.g. after an interval or row-size change). */
  requestFit(): void {
    this.forceFit = true;
  }

  destroy(): void {
    this.observer.disconnect();
    this.marks.setMarks([]);
    this.chart.destroy?.();
  }

  /** Single-day mode: one live snapshot window. */
  apply(snap: ChartSnapshot): void {
    const ch = snap.chart;
    if (!ch) return;
    this.multi = false;
    this.render([{ key: "", bars: ch.bars, cvdOffset: ch.cvd_offset }], ch.row_size);
  }

  /** Multi-day mode: sessions oldest first, today last. */
  applySessions(sessions: FootprintSession[], rowSize: number): void {
    this.multi = true;
    this.render(sessions.map((s) => ({ key: s.date, bars: s.bars, cvdOffset: s.cvdStart, live: s.live })), rowSize);
  }

  private render(groups: Group[], rowSize: number): void {
    this.rowSize = rowSize;
    const range = this.safeRange();
    const following = !this.prevTimes.length || !range || range.to >= this.prevTimes.length - 1.5;

    const usable = groups.map((g) => ({ ...g, bars: g.bars.filter(hasOhlc) }));
    const last = usable.at(-1);
    const keep = new Set(usable.slice(0, -1).map((g) => g.key));
    for (const [key, fp] of this.layers) {
      if (!keep.has(key)) {
        (this.chart as unknown as { removePrimitive(p: unknown): void }).removePrimitive(fp);
        this.layers.delete(key);
      }
    }
    this.bars = [];
    this.dateOf = new Map();
    const candles = [];
    this.table.setBars(usable.flatMap((g) => g.bars));
    for (const g of usable) {
      const fpBars = g.bars.map((b) => toFootprintBar(b, rowSize));
      const fp = g === last ? this.footprint : this.layerFor(g.key);
      fp.setOptions({ cvdOffset: g.cvdOffset, tickSize: rowSize });
      fp.setBars(fpBars);
      this.bars.push(...fpBars);
      candles.push(...g.bars.map(toCandle));
      if (this.multi) for (const b of g.bars) this.dateOf.set(b.time, g.key);
    }
    if (!last) this.footprint.setBars([]);
    const times = this.bars.map((b) => b.time);
    this.series.setData(candles);
    this.marks.setMarks(this.multi ? sessionStarts(usable.map((g) => ({
      date: g.key, live: !!g.live, bars: g.bars, cvdStart: g.cvdOffset }))) : []);

    if (times.length) {
      const newBar = times[times.length - 1] !== this.prevTimes[this.prevTimes.length - 1];
      // columns added (earlier sessions) or dropped (the live window) on the left
      let offset = 0;
      if (range && this.prevTimes.length) {
        const at = times.indexOf(this.prevTimes[0]);
        offset = at >= 0 ? at : -this.prevTimes.filter((t) => t < times[0]).length;
      }
      if (this.forceFit || (newBar && following)) {
        this.fitColumns();
        this.forceFit = false;
      } else if (range && offset) {
        // keep the columns being inspected under the same part of the screen
        this.chart.setVisibleLogicalRange({ from: range.from + offset, to: range.to + offset });
      }
    }
    this.prevTimes = times;
    this.empty.hidden = this.bars.length > 0;

    const stats = this.footprint.stats().at(-1);
    this.onStats(stats ? { delta: stats.delta, cvd: stats.cvd, volume: stats.volume } : null);
    setTimeout(() => this.checkNearStart(), 0);
  }

  private layerFor(key: string): Footprint {
    let fp = this.layers.get(key);
    if (!fp) {
      fp = this.newFootprint();
      fp.setOptions({ volumeDivisor: this.divisor(), statsRows: this.cardRows(),
                      ...FootprintController.displayOptions(this.display) });
      fp.autoscaleInfo = () => null;              // the primary layer scales on every visible column
      this.chart.addPrimitive(fp);
      this.layers.set(key, fp);
    }
    return fp;
  }

  /** True while fewer than LOAD_MORE_COLUMNS columns are left of the view. */
  nearStart(): boolean {
    const range = this.safeRange();
    return !!range && this.bars.length > 0 && range.from < LOAD_MORE_COLUMNS;
  }

  private checkNearStart(): void {
    this.onViewport(this.multi && this.nearStart());
  }

  fitColumns(): void {
    if (!this.bars.length) return;
    const width = this.chart.timeScale.width;
    const visible = Math.max(1, Math.min(this.bars.length, Math.floor(width / MIN_COLUMN_PX - 0.45)));
    this.chart.timeScale.setVisibleLogicalRange({ from: this.bars.length - visible - 0.7, to: this.bars.length - 0.25 });
  }

  private safeRange() {
    try {
      return this.chart.getVisibleLogicalRange();
    } catch {
      return null;
    }
  }

  private visibleExtent(): { min: number; max: number } | null {
    const range = this.safeRange();
    if (!range || !this.bars.length) return null;
    const from = Math.max(0, Math.floor(range.from) - 1);
    const to = Math.min(this.bars.length - 1, Math.ceil(range.to) + 1);
    const half = this.rowSize / 2;
    let min = Infinity;
    let max = -Infinity;
    for (let i = from; i <= to; i++) {
      const b = this.bars[i];
      for (const c of b.cells) {
        min = Math.min(min, c.price - half);
        max = Math.max(max, c.price + half);
      }
      if (b.low !== undefined && b.high !== undefined) {
        min = Math.min(min, b.low);
        max = Math.max(max, b.high);
      }
    }
    return Number.isFinite(min) ? { min, max } : null;
  }

  private renderTableTooltip(time: number, point: { x: number; y: number }): void {
    const b = this.table.bar(time);
    if (!b) return;
    const rows = (Object.keys(TABLE_ROW_LABELS) as TableRow[]).map((r) =>
      [TABLE_ROW_LABELS[r], cellTexts(r, rowValue(r, b), this.divisor())[0]] as [string, string]);
    this.showTooltip(time, rows, point);
  }

  private renderTooltip(hit: FootprintHover | null, point?: { x: number; y: number }): void {
    const tip = this.tooltip;
    if (!hit || !point) {
      tip.hidden = true;
      return;
    }
    const d = this.divisor();
    const q = (v: number) => (v / d).toLocaleString("en-US", { maximumFractionDigits: 4 });
    const sq = (v: number) => (v > 0 ? "+" : "") + q(v);
    const rows: [string, string][] = [];
    if (hit.cell) {
      const [lo, hi] = bandRange(hit.cell.price, this.rowSize, this.units.tickSize);
      rows.push(["Price", lo.toFixed(2) + (hi > lo ? ` – ${hi.toFixed(2)}` : "")]);
      rows.push(["Sell × Buy", `${q(hit.cell.bidVol)} × ${q(hit.cell.askVol)}`]);
    }
    rows.push(["Volume", q(hit.stats.volume)], ["Buy", q(hit.stats.askVolume)], ["Sell", q(hit.stats.bidVolume)],
      ["Delta", sq(hit.stats.delta)], ["Delta %", `${hit.stats.deltaPct.toFixed(1)}%`],
      [this.multi ? "Session CVD" : "CVD", sq(hit.stats.cvd)],
      ["Min / max Δ", hit.stats.minDelta === null ? "—" : `${sq(hit.stats.minDelta)} / ${sq(hit.stats.maxDelta ?? 0)}`],
      ["Trades", hit.stats.trades == null ? "—" : String(hit.stats.trades)]);

    this.showTooltip(hit.time, rows, point);
  }

  private showTooltip(time: number, rows: [string, string][], point: { x: number; y: number }): void {
    const tip = this.tooltip;
    // DOM nodes and textContent only: no HTML strings.
    const title = document.createElement("div");
    title.className = "mb-1 text-fg-2";
    const date = this.dateOf.get(time);
    title.textContent = `${date ? `${date} ` : ""}${istTime(time)} IST · ${this.units.units === "lots" ? `lots (${this.units.lotSize}/lot)` : "quantity"}`;
    const body = rows.map(([k, v]) => {
      const row = document.createElement("div");
      row.className = "flex justify-between gap-4";
      const key = document.createElement("span");
      key.className = "text-muted";
      key.textContent = k;
      const val = document.createElement("span");
      val.className = "num";
      val.textContent = v;
      row.append(key, val);
      return row;
    });
    tip.replaceChildren(title, ...body);
    tip.hidden = false;
    const box = this.container.getBoundingClientRect();
    tip.style.left = `${Math.max(4, Math.min(point.x + 14, box.width - tip.offsetWidth - 6))}px`;
    tip.style.top = `${Math.max(4, Math.min(point.y + 14, box.height - tip.offsetHeight - 6))}px`;
  }
}
