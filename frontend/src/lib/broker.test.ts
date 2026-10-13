import { describe, expect, it } from "vitest";

import { brokerLabel } from "./broker";
import type { BrokerStatus } from "./types";

const base: BrokerStatus = { state: "ready", feed_on: true, logged_in_at: "2026-10-14T08:30:04+05:30",
                             next_action: "feed off", next_action_at: "2026-10-14T15:45:00+05:30" };

describe("broker label", () => {
  it("says when the login happened and until when the feed runs", () => {
    const l = brokerLabel(base)!;
    expect([l.text, l.tone]).toEqual(["Broker: logged in 08:30", "ok"]);
    expect(l.title).toContain("Feed on until 15:45");
  });

  it("shows the next login while waiting or after the close", () => {
    const next = { next_action: "login", next_action_at: "2026-10-15T08:30:00+05:30", feed_on: false };
    expect(brokerLabel({ ...base, ...next, state: "waiting" })!.text).toBe("Broker: waiting · next login Thu 08:30 IST");
    expect(brokerLabel({ ...base, ...next, state: "stopped" })!.text).toBe("Broker: feed off · next login Thu 08:30 IST");
  });

  it("flags a failed login; admins see why in the tooltip", () => {
    const l = brokerLabel({ ...base, state: "failed", feed_on: false, next_action: "manual re-login", next_action_at: null,
                            last_error: "RuntimeError: Invalid totp (AB1050)", attempts: 21 })!;
    expect([l.text, l.tone]).toEqual(["Broker: login failed", "down"]);
    expect(l.title).toContain("Invalid totp");
    expect(l.title).toContain("Attempts today: 21");
    // a trader's view carries no error text: a generic hint instead
    expect(brokerLabel({ ...base, state: "failed", feed_on: false })!.title).toContain("Re-login");
  });

  it("is absent without a scheduled session", () => {
    expect(brokerLabel(null)).toBeNull();
  });
});
