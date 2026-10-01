// Alert condition metadata for forms and labels. The server validates every
// rule (backend/app/domain/alerts/rules.py); this only drives the UI.
import type { AlertKind, AlertRuleInput } from "./types";

export interface KindInfo {
  label: string;
  level?: "price" | "signed" | "positive";   // has a numeric level, and of what sort
  candle?: boolean;                          // evaluated when a candle closes (needs an interval)
  sides?: { value: string; label: string }[];
}

export const KIND_INFO: Record<AlertKind, KindInfo> = {
  price_above: { label: "Price crosses above", level: "price" },
  price_below: { label: "Price crosses below", level: "price" },
  cvd_above: { label: "CVD crosses above", level: "signed" },
  cvd_below: { label: "CVD crosses below", level: "signed" },
  candle_delta_above: { label: "Candle delta at least", level: "signed", candle: true },
  candle_delta_below: { label: "Candle delta at most", level: "signed", candle: true },
  candle_volume_above: { label: "Candle volume at least", level: "positive", candle: true },
  stacked_imbalance: {
    label: "Stacked imbalance", candle: true,
    sides: [{ value: "any", label: "Buy or sell" }, { value: "buy", label: "Buy" }, { value: "sell", label: "Sell" }],
  },
  value_area_break: {
    label: "Close outside previous value area", candle: true,
    sides: [{ value: "any", label: "Either side" }, { value: "up", label: "Above VAH" }, { value: "down", label: "Below VAL" }],
  },
};

export const KIND_ORDER = Object.keys(KIND_INFO) as AlertKind[];

export interface RuleDraft {
  name: string;
  kind: AlertKind;
  level: string;
  interval: number;
  side: string;
  mode: "once" | "repeat";
  cooldownSec: number;
  channelIds: string[];
}

/** The API payload for a draft, or an error message for the form. */
export function draftToInput(d: RuleDraft): AlertRuleInput | string {
  const info = KIND_INFO[d.kind];
  const params: AlertRuleInput["params"] = {};
  if (info.level) {
    const level = Number(d.level);
    if (d.level.trim() === "" || !Number.isFinite(level)) return "Enter a number for the level";
    if (info.level !== "signed" && level <= 0) return "The level must be positive";
    params.level = level;
  }
  if (info.candle) params.interval = d.interval;
  if (info.sides) params.side = d.side;
  const name = d.name.trim() || info.label;
  return { name: name.slice(0, 80), kind: d.kind, params, mode: d.mode, cooldown_sec: d.cooldownSec,
           channel_ids: d.channelIds };
}
