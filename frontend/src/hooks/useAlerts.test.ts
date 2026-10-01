import { beforeEach, describe, expect, it } from "vitest";

import { draftToInput, type RuleDraft } from "@/lib/alerts";
import type { AlertEvent, AlertRule } from "@/lib/types";
import { alertActions, useAlertStore } from "./useAlerts";

const event = (id: number, rule_id = "r1"): AlertEvent => ({
  id, rule_id, rule_name: "Breakout", fired_at: "2026-10-01T04:00:00Z", kind: "price_above", symbol: "NIFTY",
  message: "Breakout: price crosses above 24500", value: 24500, details: null, delivery: null, suppressed: null,
  read_at: null,
});

const rule = (mode: AlertRule["mode"]): AlertRule => ({
  id: "r1", name: "Breakout", kind: "price_above", params: { level: 24500 }, description: "", mode,
  cooldown_sec: 60, channel_ids: [], enabled: true, revision: 1, fire_count: 0, last_fired_at: null,
  created_at: "", updated_at: "",
});

describe("alerts store", () => {
  beforeEach(() => useAlertStore.setState({ events: [], toasts: [], unread: 0, rules: [] }));

  it("adds live alerts to history, unread and toasts once, keeping at most 3 toasts", () => {
    useAlertStore.setState({ rules: [rule("once")] });
    for (const id of [1, 2, 3, 4]) alertActions.receive(event(id));
    alertActions.receive(event(4));                                   // duplicate (e.g. after a reconnect)
    const s = useAlertStore.getState();
    expect(s.events.map((e) => e.id)).toEqual([4, 3, 2, 1]);
    expect(s.unread).toBe(4);
    expect(s.toasts.map((e) => e.id)).toEqual([4, 3, 2]);
    expect(s.rules[0]).toMatchObject({ fire_count: 4, enabled: false });   // a "once" rule pauses itself
    alertActions.dismissToast(3);
    expect(useAlertStore.getState().toasts.map((e) => e.id)).toEqual([4, 2]);
  });
});

describe("rule drafts", () => {
  const draft: RuleDraft = { name: " ", kind: "price_above", level: "24500.5", interval: 300, side: "any",
                             mode: "repeat", cooldownSec: 60, channelIds: ["c"] };

  it("builds only the params the condition needs", () => {
    expect(draftToInput(draft)).toEqual({ name: "Price crosses above", kind: "price_above", params: { level: 24500.5 },
                                          mode: "repeat", cooldown_sec: 60, channel_ids: ["c"] });
    expect(draftToInput({ ...draft, kind: "stacked_imbalance", side: "sell" })).toMatchObject(
      { params: { interval: 300, side: "sell" } });
    expect(draftToInput({ ...draft, kind: "cvd_below", level: "-5000" })).toMatchObject({ params: { level: -5000 } });
  });

  it("rejects missing or impossible levels", () => {
    expect(draftToInput({ ...draft, level: "" })).toBe("Enter a number for the level");
    expect(draftToInput({ ...draft, level: "-1" })).toBe("The level must be positive");
    expect(draftToInput({ ...draft, kind: "candle_volume_above", level: "abc" })).toBe("Enter a number for the level");
  });
});
