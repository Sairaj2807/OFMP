// Minimal typings for the Footprint primitive of OpenAlgo Charts 2.4.0.

export interface FootprintCell {
  price: number;
  bidVol: number;
  askVol: number;
}

export interface FootprintBar {
  time: number;
  cells: FootprintCell[];
  delta: number;
  rowSize: number;
  open?: number;
  high?: number;
  low?: number;
  close?: number;
  tradeCount?: number;
  minDelta?: number;
  maxDelta?: number;
}

export interface FootprintStats {
  time: number;
  volume: number;
  askVolume: number;
  bidVolume: number;
  delta: number;
  deltaPct: number;
  minDelta: number | null;
  maxDelta: number | null;
  cvd: number;
  trades: number | null;
  poc: number;
  vah: number;
  val: number;
}

export interface FootprintHover {
  time: number;
  cell?: FootprintCell;
  stats: FootprintStats;
}

export declare class Footprint {
  constructor(options?: Record<string, unknown>);
  setOptions(options: Record<string, unknown>): void;
  setBars(bars: FootprintBar[]): void;
  stats(): FootprintStats[];
  hoverAt(x: number, y: number): FootprintHover | null;
  autoscaleInfo(): { min: number; max: number } | null;
}

export declare function compactVol(value: number): string;
