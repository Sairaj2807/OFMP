// Shapes of the backend's /api/v1 responses and /ws/v1/stream messages.

export interface User {
  id: string;
  email: string;
  display_name: string | null;
  role: string;
  email_verified: boolean;
  permissions?: string[];
}

export interface Contract {
  token: string | null;
  tradingsymbol: string;
  name?: string;
  expiry?: string;
  tick_size: number;
  lotsize: number;
  exch_seg?: string;
}

/** One footprint column as the engine sends it (server._chart_payload). */
export interface EngineBar {
  time: number;
  open?: number | null;
  high?: number | null;
  low?: number | null;
  close?: number | null;
  delta: number;
  min_delta?: number | null;
  max_delta?: number | null;
  trades?: number | null;
  cells: { price: number; buy: number; sell: number }[];
}

export type BookLevel = [price: number, qty: number, orders: number];

export interface ChartSnapshot {
  ready: boolean;
  mode?: "live" | "replay";
  status?: {
    connected: boolean;
    error: string | null;
    tick_count: number;
    symbol: string;
    tick_size: number;
    lot_size: number;
  };
  source?: { kind: string; side_basis: string };
  book?: {
    bids: BookLevel[];
    asks: BookLevel[];
    spread: number | null;
    mid: number | null;
    micro: number | null;
    obi: number;
  };
  quote?: {
    ltp: number;
    open: number | null;
    high: number | null;
    low: number | null;
    close: number | null;
    volume: number;
    oi: number | null;
  } | null;
  chart?: { row_size: number; interval_sec: number; cvd_offset: number; bars: EngineBar[] };
}

export interface ChartSettings {
  ppr: number;
  interval: number;
}

export interface ServerMessage {
  type: "welcome" | "subscribed" | "unsubscribed" | "snapshot" | "ping" | "pong" | "error";
  seq: number;
  ts: number;
  id?: string;
  data?: unknown;
}
