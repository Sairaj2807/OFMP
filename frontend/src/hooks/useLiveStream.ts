"use client";

// Owns the single /ws/v1/stream connection for the terminal: publishes
// snapshots to the snapshot bus, keeps one subscription per visible chart in
// sync with its settings, and refreshes the session when the socket reports
// it expired (close 4401).
import { useEffect, useRef } from "react";
import { create } from "zustand";
import { useShallow } from "zustand/react/shallow";

import { api } from "@/lib/api";
import { snapshotBus } from "@/lib/snapshots";
import { StreamClient, defaultStreamUrl } from "@/lib/stream";
import { useSession } from "@/stores/session";
import { CHART_IDS, useTerminal } from "@/stores/terminal";

export interface ChartStats {
  delta: number;
  cvd: number;
  volume: number;
}

/** Per-chart figures for panels outside the chart (bottom bar). */
export const useLiveStats = create<{ stats: Record<string, ChartStats | null>; set: (id: string, s: ChartStats | null) => void }>()(
  (set) => ({
    stats: {},
    set: (id, s) => set((st) => ({ stats: { ...st.stats, [id]: s } })),
  }),
);

export function useLiveStream(enabled: boolean): void {
  const client = useRef<StreamClient | null>(null);
  const subscribed = useRef(new Map<string, string>()); // id -> settings key
  const charts = useTerminal(useShallow((s) => s.charts.slice(0, s.layout).map((c) => `${c.id}:${c.ppr}:${c.interval}`)));

  useEffect(() => {
    if (!enabled) return;
    const { setStream, setFeed, setIntervals } = useSession.getState();
    const c = new StreamClient({
      url: defaultStreamUrl,
      onSnapshot: (id, snap) => {
        snapshotBus.publish(id, snap);
        if (snap.status) {
          setFeed({ connected: snap.status.connected, error: snap.status.error, symbol: snap.status.symbol,
                    tickSize: snap.status.tick_size, lotSize: snap.status.lot_size });
        }
      },
      onWelcome: (data) => data.intervals && setIntervals(data.intervals),
      onStatus: (status) => setStream(status),
      onUnauthenticated: () => api.refresh(),
    });
    client.current = c;
    subscribed.current.clear();
    c.start();
    return () => {
      c.stop();
      client.current = null;
    };
  }, [enabled]);

  useEffect(() => {
    const c = client.current;
    if (!c) return;
    const wanted = new Map(charts.map((key) => [key.split(":")[0], key]));
    for (const [id, key] of wanted) {
      if (subscribed.current.get(id) !== key) {
        const [, ppr, interval] = key.split(":").map(Number);
        c.subscribe(id, { ppr, interval });
        subscribed.current.set(id, key);
      }
    }
    for (const id of CHART_IDS) {
      if (!wanted.has(id) && subscribed.current.has(id)) {
        c.unsubscribe(id);
        subscribed.current.delete(id);
        snapshotBus.forget(id);
      }
    }
  }, [charts, enabled]);
}
