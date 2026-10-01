"use client";

// Alerts in the terminal: the user's rules, webhook channels, recent history
// and unread count, plus toasts for alerts arriving over the live stream.
//
// - Loaded once on sign-in (useAlerts); without a database (503) the alerts UI
//   stays hidden.
// - Live alerts arrive as "alert" stream messages (useLiveStream ->
//   alertActions.receive): prepended to the history, counted unread, toasted,
//   and shown as a browser notification when the tab is hidden and the user
//   has allowed notifications.
import { useEffect } from "react";
import { create } from "zustand";

import { ApiError, api } from "@/lib/api";
import type { AlertChannel, AlertEvent, AlertKinds, AlertRule, AlertRuleInput } from "@/lib/types";

const HISTORY = 50;
const MAX_TOASTS = 3;
export const TOAST_MS = 10_000;

export type AlertTab = "history" | "rules" | "webhooks";

interface AlertState {
  available: boolean;
  open: boolean;              // the alerts panel
  tab: AlertTab;
  kinds: AlertKinds | null;
  rules: AlertRule[];
  channels: AlertChannel[];
  events: AlertEvent[];
  unread: number;
  toasts: AlertEvent[];
  set: (patch: Partial<Omit<AlertState, "set">>) => void;
}

export const useAlertStore = create<AlertState>()((set) => ({
  available: false,
  open: false,
  tab: "history",
  kinds: null,
  rules: [],
  channels: [],
  events: [],
  unread: 0,
  toasts: [],
  set: (patch) => set(patch),
}));

const store = () => useAlertStore.getState();

async function load() {
  try {
    const [kinds, rules, channels, events, { unread }] = await Promise.all([
      api.alertKinds(), api.alertRules(), api.alertChannels(), api.alertEvents(HISTORY), api.alertUnreadCount(),
    ]);
    store().set({ available: true, kinds, rules, channels, events, unread });
  } catch (e) {
    store().set({ available: false });
    if (!(e instanceof ApiError && e.status === 503)) throw e;
  }
}

const replaceRule = (rule: AlertRule) =>
  store().set({ rules: store().rules.map((r) => (r.id === rule.id ? rule : r)) });

function notifyBrowser(event: AlertEvent) {
  try {
    if (typeof Notification === "undefined" || Notification.permission !== "granted" || !document.hidden) return;
    new Notification(event.rule_name ?? "Alert", { body: event.message, tag: `ofmp-alert-${event.id}` });
  } catch {
    /* notifications unsupported in this context */
  }
}

export const alertActions = {
  load,

  receive(event: AlertEvent) {
    const s = store();
    if (s.events.some((e) => e.id === event.id)) return;          // already have it (reconnect, refetch)
    s.set({
      events: [event, ...s.events].slice(0, HISTORY),
      unread: s.unread + 1,
      toasts: [event, ...s.toasts].slice(0, MAX_TOASTS),
      rules: s.rules.map((r) => (r.id === event.rule_id
        ? { ...r, fire_count: r.fire_count + 1, last_fired_at: event.fired_at,
            enabled: r.mode === "once" ? false : r.enabled }
        : r)),
    });
    notifyBrowser(event);
  },

  dismissToast(id: number) {
    store().set({ toasts: store().toasts.filter((t) => t.id !== id) });
  },

  async markAllRead() {
    await api.markAlertsRead();
    const now = new Date().toISOString();
    store().set({ unread: 0, events: store().events.map((e) => (e.read_at ? e : { ...e, read_at: now })) });
  },

  async createRule(input: AlertRuleInput) {
    const rule = await api.createAlertRule(input);
    store().set({ rules: [...store().rules, rule] });
    return rule;
  },

  async setEnabled(rule: AlertRule, enabled: boolean) {
    try {
      replaceRule(await api.updateAlertRule(rule.id, rule.revision, { enabled }));
    } catch (e) {
      // changed in another window: a pause/resume is safe to apply to the newer copy
      if (!(e instanceof ApiError && e.code === "REVISION_CONFLICT")) throw e;
      const fresh = (await api.alertRules()).find((r) => r.id === rule.id);
      if (!fresh) throw e;
      replaceRule(await api.updateAlertRule(fresh.id, fresh.revision, { enabled }));
    }
  },

  async deleteRule(id: string) {
    await api.deleteAlertRule(id);
    store().set({ rules: store().rules.filter((r) => r.id !== id) });
  },

  /** Returns the signing secret, which the server shows only once. */
  async createChannel(name: string, url: string) {
    const { secret, ...channel } = await api.createAlertChannel(name, url);
    store().set({ channels: [...store().channels, channel] });
    return secret;
  },

  async deleteChannel(id: string) {
    await api.deleteAlertChannel(id);
    store().set({
      channels: store().channels.filter((c) => c.id !== id),
      rules: store().rules.map((r) => ({ ...r, channel_ids: r.channel_ids.filter((c) => c !== id) })),
    });
  },

  async testChannel(id: string) {
    const result = await api.testAlertChannel(id);
    store().set({ channels: await api.alertChannels() });
    return result;
  },

  async requestBrowserNotifications(): Promise<NotificationPermission | "unsupported"> {
    if (typeof Notification === "undefined") return "unsupported";
    return Notification.permission === "default" ? Notification.requestPermission() : Notification.permission;
  },
};

export function useAlerts(enabled: boolean): void {
  useEffect(() => {
    if (!enabled) return;
    load().catch(() => undefined);
  }, [enabled]);
}
