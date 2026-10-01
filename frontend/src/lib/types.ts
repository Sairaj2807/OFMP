// Shapes of the backend's /api/v1 responses and /ws/v1/stream messages.

export interface User {
  id: string;
  email: string;
  display_name: string | null;
  role: string;
  email_verified: boolean;
  permissions?: string[];
}

/** Exchange session state (holiday calendar applied), from /api/v1/market/status. */
export interface MarketSession {
  exchange: string;
  open: boolean;
  session: { open: string; close: string; name: string | null } | null;
  holiday: string | null;
  next_open: string | null;
  calendar_covers_today: boolean;
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

export type ChartView = "footprint" | "profile";

export interface ChartSnapshot {
  ready: boolean;
  mode?: "live" | "replay";
  view?: "profile";              // absent on footprint snapshots
  profile?: MarketProfile;
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
  replay?: ReplayMeta;
}

export interface ReplayMeta {
  date: string;
  start_ms: number;
  end_ms: number;
  cursor_ms: number;
  index: number;
  total: number;
  playing: boolean;
  speed: number;
  speeds: number[];
}

export interface ReplaySessionInfo {
  date: string;
  trades: number | null;
}

export interface WorkspaceSummary {
  id: string;
  name: string;
  config_version: number;
  revision: number;
  is_default: boolean;
  created_at: string;
  updated_at: string;
}

export interface Workspace extends WorkspaceSummary {
  config: Record<string, unknown>;
}

export interface ChartSettings {
  ppr: number;
  interval: number;
  view?: ChartView;
  row?: number;                  // market profile row size, points
}

/** One price row of a market profile (high to low in MarketProfile.rows). */
export interface ProfileRow {
  price: number;                 // row floor
  letters: string;               // one letter per period whose high-low range covers the row
  tpo: number;
  volume: number;
  buy: number;
  sell: number;
  delta: number;
}

export interface MarketProfileLevels {
  date: string;
  poc_tpo: number | null;
  poc_volume: number | null;
  vah: number | null;
  val: number | null;
  high: number;
  low: number;
}

/** A session's market profile (backend/app/domain/profile). */
export interface MarketProfile {
  date: string | null;
  empty?: boolean;
  row_size?: number;
  tick_size?: number;
  period_minutes?: number;
  periods?: { index: number; letter: string; start_ms: number; high: number; low: number }[];
  current_period?: number | null;
  rows: ProfileRow[];
  poc_tpo?: number | null;
  poc_volume?: number | null;
  value_area_tpo?: { low: number; high: number } | null;
  value_area_volume?: { low: number; high: number } | null;
  value_area_pct?: number;
  initial_balance?: { high: number | null; low: number | null; final: boolean; range: number | null;
                      extension_up: number | null; extension_down: number | null };
  single_prints?: { low: number; high: number; letter: string }[];
  tails?: { low: number; high: number; letter: string; kind: "buying" | "selling" }[];
  hvn?: number[];
  lvn?: number[];
  stats?: { open: number; high: number; low: number; close: number; tpo_total: number; volume: number;
            delta: number; trades: number };
  previous?: MarketProfileLevels | null;
}

export interface ServerMessage {
  type: "welcome" | "subscribed" | "replay_started" | "unsubscribed" | "snapshot" | "ping" | "pong" | "error" | "alert";
  seq: number;
  ts: number;
  id?: string;
  data?: unknown;
}

// -- alerts ------------------------------------------------------------------

export type AlertKind =
  | "price_above" | "price_below" | "cvd_above" | "cvd_below"
  | "candle_delta_above" | "candle_delta_below" | "candle_volume_above" | "stacked_imbalance" | "value_area_break";

export interface AlertRule {
  id: string;
  name: string;
  kind: AlertKind;
  params: { level?: number; interval?: number; side?: string };
  description: string;
  mode: "once" | "repeat";
  cooldown_sec: number;
  channel_ids: string[];
  enabled: boolean;
  revision: number;
  fire_count: number;
  last_fired_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface AlertRuleInput {
  name: string;
  kind: AlertKind;
  params: AlertRule["params"];
  mode: AlertRule["mode"];
  cooldown_sec: number;
  channel_ids: string[];
}

export interface AlertEvent {
  id: number;
  rule_id: string;
  rule_name?: string;
  fired_at: string;
  kind: AlertKind;
  symbol: string | null;
  message: string;
  value: number | null;
  details: Record<string, unknown> | null;
  delivery: Record<string, string> | null;
  suppressed: string | null;
  read_at: string | null;
}

export interface AlertChannel {
  id: string;
  kind: "webhook";
  name: string;
  url: string | null;
  enabled: boolean;
  last_status: "ok" | "failed" | null;
  last_error: string | null;
  last_delivery_at: string | null;
  created_at: string;
}

export interface AlertKinds {
  trade_kinds: AlertKind[];
  candle_kinds: AlertKind[];
  modes: AlertRule["mode"][];
  intervals: number[];
  cooldown_sec: { min: number; max: number; default: number };
  max_rules: number;
  max_channels: number;
}
