// Imperative footprint chart controller: owns the chart-library objects and
// applies snapshots to them directly. Kept out of React so that a 2 Hz stream
// never re-renders components; FootprintChart only creates it, forwards
// setting changes to it, and destroys it. Behaviour ported from
// static/orderflow.js (autoscale on visible columns, follow the right edge,
// keep the inspected columns in place while data updates).
import type { Chart, Series } from "@/vendor/openalgo-charts/openalgo-charts.mjs";
import type { Footprint, FootprintBar, FootprintHover } from "@/vendor/openalgo-charts/openalgo-charts.profile.mjs";

import { bandRange, hasOhlc, toCandle, toFootprintBar } from "../adapter";
import { istTime } from "../format";
import type { ChartSnapshot } from "../types";

export const COLORS = {
  bg: "#12151b", grid: "#181c23", border: "#252b35", text: "#a3acba",
  buy: "#1fb58f", sell: "#e5485d", poc: "#dfe4ec", va: "#5d6b80", cell: "#eef3fa",
};
const TRANSPARENT = "rgba(0,0,0,0)";
const MIN_COLUMN_PX = 125;

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
  private readonly footprint: Footprint;
  private readonly series: Series;
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
    this.footprint = new libs.profile.Footprint({
      textColorMode: "contrast", statsPosition: "bar", statsRows: ["volume", "delta", "deltaPct"], tableRows: [],
      pocStyle: "outline", valueAreaPercent: 0.7, imbalanceRatio: 3,
      widthFactor: 0.84, font: 11, radius: 1, statsRowHeight: 18, volumeDivisor: this.divisor(),
      buyColor: COLORS.buy, sellColor: COLORS.sell, pocColor: COLORS.poc, valueAreaColor: COLORS.va,
      textColor: COLORS.cell, buyTextColor: "#8aefcd", sellTextColor: "#ffb2bd",
      ...FootprintController.displayOptions(display),
    });
    this.chart.addPrimitive(this.footprint);

    // Autoscale on the columns in (or next to) view, not every loaded column.
    const loadedExtent = this.footprint.autoscaleInfo.bind(this.footprint);
    this.footprint.autoscaleInfo = () => this.visibleExtent() ?? loadedExtent();

    this.chart.on("crosshair:move", (e) =>
      this.renderTooltip(e.point ? this.footprint.hoverAt(e.point.x, e.point.y) : null, e.point));

    this.width = container.clientWidth;
    this.observer = new ResizeObserver(() => {
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

  private divisor(): number {
    return this.units.units === "lots" ? this.units.lotSize : 1;
  }

  setDisplay(display: DisplayOptions): void {
    this.footprint.setOptions(FootprintController.displayOptions(display));
  }

  setUnits(units: UnitOptions): void {
    this.units = units;
    this.footprint.setOptions({ volumeDivisor: this.divisor() });
  }

  /** Refit the columns on the next snapshot (e.g. after an interval or row-size change). */
  requestFit(): void {
    this.forceFit = true;
  }

  destroy(): void {
    this.observer.disconnect();
    this.chart.destroy?.();
  }

  apply(snap: ChartSnapshot): void {
    const ch = snap.chart;
    if (!ch) return;
    this.rowSize = ch.row_size;
    const range = this.safeRange();
    const following = !this.prevTimes.length || !range || range.to >= this.prevTimes.length - 1.5;

    const usable = ch.bars.filter(hasOhlc);
    this.bars = usable.map((b) => toFootprintBar(b, ch.row_size));
    const times = this.bars.map((b) => b.time);
    this.series.setData(usable.map(toCandle));
    this.footprint.setOptions({ cvdOffset: ch.cvd_offset, tickSize: ch.row_size });
    this.footprint.setBars(this.bars);

    if (times.length) {
      const newBar = times[times.length - 1] !== this.prevTimes[this.prevTimes.length - 1];
      if (this.forceFit || (newBar && following)) {
        this.fitColumns();
        this.forceFit = false;
      } else if (!following && range && this.prevTimes.length) {
        // keep the columns being inspected under the same part of the screen
        const at = times.indexOf(this.prevTimes[0]);
        const offset = at >= 0 ? at : -this.prevTimes.filter((t) => t < times[0]).length;
        if (offset) this.chart.setVisibleLogicalRange({ from: range.from + offset, to: range.to + offset });
      }
    }
    this.prevTimes = times;
    this.empty.hidden = this.bars.length > 0;

    const stats = this.footprint.stats().at(-1);
    this.onStats(stats ? { delta: stats.delta, cvd: stats.cvd, volume: stats.volume } : null);
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
      ["Delta", sq(hit.stats.delta)], ["Delta %", `${hit.stats.deltaPct.toFixed(1)}%`], ["CVD", sq(hit.stats.cvd)],
      ["Min / max Δ", hit.stats.minDelta === null ? "—" : `${sq(hit.stats.minDelta)} / ${sq(hit.stats.maxDelta ?? 0)}`],
      ["Trades", hit.stats.trades == null ? "—" : String(hit.stats.trades)]);

    // DOM nodes and textContent only: no HTML strings.
    const title = document.createElement("div");
    title.className = "mb-1 text-fg-2";
    title.textContent = `${istTime(hit.time)} IST · ${this.units.units === "lots" ? `lots (${this.units.lotSize}/lot)` : "quantity"}`;
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
