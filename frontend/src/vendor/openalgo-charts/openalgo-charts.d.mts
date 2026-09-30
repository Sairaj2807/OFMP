// Minimal typings for the parts of OpenAlgo Charts 2.4.0 (Apache-2.0, see
// LICENSE/NOTICE) that the terminal uses. The library ships as a prebuilt ES
// module copied from static/vendor/openalgo-charts/.

export interface ChartTheme {
  [key: string]: unknown;
}

export interface LogicalRange {
  from: number;
  to: number;
}

export interface CrosshairEvent {
  point?: { x: number; y: number };
}

export interface CandleData {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume?: number;
}

export interface Series {
  setData(data: CandleData[]): void;
}

export interface Chart {
  addSeries(type: "candlestick", options?: Record<string, unknown>): Series;
  addPrimitive(primitive: unknown): void;
  setTheme(theme: ChartTheme): void;
  setPriceScaleOptions(options: Record<string, unknown>): void;
  getVisibleLogicalRange(): LogicalRange | null;
  setVisibleLogicalRange(range: LogicalRange): void;
  on(event: "crosshair:move", handler: (e: CrosshairEvent) => void): void;
  timeScale: { width: number; setVisibleLogicalRange(range: LogicalRange): void };
  destroy?(): void;
  remove?(): void;
}

export declare function createChart(container: HTMLElement, options?: Record<string, unknown>): Chart;
export declare const darkTheme: ChartTheme;
export declare const lightTheme: ChartTheme;
